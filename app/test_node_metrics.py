"""Offline fixtures for GPU, capacities, location and acknowledged heartbeat RTT."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(os.environ.get('FLEET_TEST_NODE', str(Path(__file__).with_name('node-next.py'))))
if not SOURCE.exists():
    SOURCE = Path(__file__).parent / 'static/node.py'
spec = importlib.util.spec_from_file_location('node_probe_tested', SOURCE)
node = importlib.util.module_from_spec(spec)
# Collection fixtures run on Windows too; no POSIX service/lock function is called here.
imports = {name: ModuleType(name) for name in ('fcntl', 'pwd')} if sys.platform == 'win32' else {}
with patch.dict(sys.modules, imports):
    spec.loader.exec_module(node)


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.proc, self.interfaces = self.root / 'proc', self.root / 'interfaces'
        (self.proc / 'net').mkdir(parents=True)
        self.interfaces.mkdir()
        (self.proc / 'net/route').write_text('Iface Destination Gateway Flags RefCnt Use Metric Mask\n')
        (self.proc / 'net/ipv6_route').write_text('')
        self.sampler = node.NetworkSampler(self.proc, self.interfaces)

    def interface(self, name, physical=False, state='up', master=False):
        path = self.interfaces / name
        path.mkdir(exist_ok=True)
        (path / 'operstate').write_text(state)
        (path / 'ifindex').write_text(str(len(list(self.interfaces.iterdir()))))
        if physical:
            (path / 'device').mkdir(exist_ok=True)
        if master:
            (path / 'master').touch()

    def counters(self, **values):
        rows = []
        for name, supplied in values.items():
            fields = [0] * 16
            for index, value in zip((0, 8, 2, 10, 3, 11), supplied):
                fields[index] = value
            rows.append(name + ': ' + ' '.join(map(str, fields)))
        (self.proc / 'net/dev').write_text('header\nheader\n' + '\n'.join(rows) + '\n')

    def routes(self, **metrics):
        (self.proc / 'net/route').write_text('Iface Destination Gateway Flags RefCnt Use Metric Mask\n' +
            ''.join(f'{name} 00000000 01000000 0003 0 0 {metric} 00000000\n' for name, metric in metrics.items()))

    def sample(self, tick, wall=1000):
        with patch.object(node.time, 'monotonic', return_value=tick), patch.object(node.time, 'time', return_value=wall):
            return self.sampler.sample()

    def test_default_route_scope_excludes_loopback_container_and_underlying_vpn_traffic(self):
        for name in ('eth0', 'wg0', 'veth123', 'lo'):
            self.interface(name)
        self.routes(eth0=100, wg0=50)
        self.counters(eth0=(100000, 200000), wg0=(1000, 2000, 2, 3, 4, 5), veth123=(50000, 60000), lo=(9000, 9000))
        first = self.sample(10)
        self.assertEqual(first['network_interfaces'], ['wg0'])
        self.assertEqual(first['network_scope'], 'default_route')
        self.assertEqual(first['network_status'], 'collecting')
        self.assertIsNone(first['network_rx_bps'])
        self.assertEqual((first['network_rx'], first['network_tx']), (1000, 2000))
        self.counters(eth0=(900000, 800000), wg0=(6000, 3000, 2, 4, 4, 6), veth123=(800000, 800000))
        second = self.sample(20, wall=1010)
        self.assertEqual((second['network_rx_bps'], second['network_tx_bps']), (500, 100))
        self.assertEqual(second['network_interval_seconds'], 10)
        self.assertEqual((second['network_rx_errors'], second['network_tx_errors'],
                          second['network_rx_dropped'], second['network_tx_dropped']), (2, 4, 4, 6))
        self.assertEqual(second['network_counter_scope'], 'interface_lifetime')

    def test_ipv6_default_is_used_when_ipv4_default_is_absent(self):
        self.interface('ens6')
        self.counters(ens6=(100, 200))
        (self.proc / 'net/ipv6_route').write_text(' '.join([
            '0' * 32, '00', '0' * 32, '00', '0' * 32, '00000064', '0', '0', '00000001', 'ens6']) + '\n')
        result = self.sample(10)
        self.assertEqual(result['network_interfaces'], ['ens6'])
        self.assertEqual(result['network_scope'], 'default_route')

    def test_fallback_uses_active_physical_interfaces_without_bond_slaves(self):
        self.interface('eth0', physical=True)
        self.interface('eth1', physical=True, master=True)
        self.interface('wlan0', physical=True, state='down')
        self.interface('veth123')
        self.counters(eth0=(100, 200), eth1=(999, 999), wlan0=(999, 999), veth123=(999, 999))
        result = self.sample(10)
        self.assertEqual(result['network_interfaces'], ['eth0'])
        self.assertEqual(result['network_scope'], 'physical')
        self.assertEqual(result['network_rx'], 100)

    def test_container_fallback_excludes_virtual_bridges(self):
        for name in ('eth0', 'eth0.100', 'macvlan0', 'docker0', 'br0', 'veth12'):
            self.interface(name)
        (self.interfaces / 'br0/bridge').mkdir()
        self.counters(**{'eth0': (100, 200), 'eth0.100': (999, 999), 'macvlan0': (999, 999),
                         'docker0': (999, 999), 'br0': (999, 999), 'veth12': (999, 999)})
        result = self.sample(10)
        self.assertEqual(result['network_interfaces'], ['eth0'])
        self.assertEqual(result['network_scope'], 'non_virtual')

    def test_same_name_recreated_interface_does_not_create_rate_spike(self):
        self.interface('eth0')
        self.counters(eth0=(100, 200))
        self.sample(10)
        (self.interfaces / 'eth0/ifindex').write_text('99')
        self.counters(eth0=(1000000, 2000000))
        recreated = self.sample(20)
        self.assertEqual(recreated['network_status'], 'reset')
        self.assertIsNone(recreated['network_rx_bps'])

    def test_per_interface_counter_reset_does_not_hide_inside_increasing_aggregate(self):
        for name in ('eth0', 'eth1'):
            self.interface(name, physical=True)
        self.counters(eth0=(1000, 1000), eth1=(1000, 1000))
        self.sample(10)
        self.counters(eth0=(10, 10), eth1=(3000, 3000))
        reset = self.sample(20)
        self.assertEqual(reset['network_rx'], 3010)
        self.assertEqual(reset['network_status'], 'reset')
        self.assertIsNone(reset['network_rx_bps'])
        self.counters(eth0=(110, 210), eth1=(3100, 3200))
        valid = self.sample(30)
        self.assertEqual((valid['network_rx_bps'], valid['network_tx_bps']), (20, 40))

    def test_interface_change_and_sampling_gap_start_new_rate_baseline(self):
        self.interface('eth0')
        self.interface('eth1')
        self.counters(eth0=(100, 200), eth1=(9000, 9000))
        self.routes(eth0=100)
        self.sample(10)
        self.routes(eth1=100)
        changed = self.sample(20)
        self.assertEqual(changed['network_status'], 'reset')
        self.assertIsNone(changed['network_rx_bps'])
        self.counters(eth0=(100, 200), eth1=(19000, 19000))
        delayed = self.sample(60)
        self.assertEqual(delayed['network_status'], 'reset')
        self.assertIsNone(delayed['network_rx_bps'])

    def test_rate_uses_monotonic_clock_while_wall_clock_moves_backwards(self):
        self.interface('eth0')
        self.counters(eth0=(100, 200))
        self.sample(10, wall=1000)
        self.counters(eth0=(1100, 2200))
        result = self.sample(20, wall=500)
        self.assertEqual((result['network_rx_bps'], result['network_tx_bps']), (100, 200))
        self.assertEqual(result['network_sampled_at'], 500)

    def test_unknown_stale_and_failed_samples_are_not_zero(self):
        self.interface('eth0')
        self.counters(eth0=(100, 200))
        self.assertIsNone(self.sample(10)['network_rx_bps'])
        valid_zero = self.sample(20)
        self.assertEqual(valid_zero['network_rx_bps'], 0)
        with patch.object(node.time, 'monotonic', return_value=56), \
                patch.object(node.pathlib.Path, 'read_text', side_effect=AssertionError('snapshot must not read files')):
            stale = self.sampler.snapshot()
        self.assertEqual(stale['network_status'], 'stale')
        self.assertIsNone(stale['network_rx_bps'])
        self.assertEqual(stale['network_rx'], 100)
        (self.proc / 'net/dev').unlink()
        error = self.sample(60)
        self.assertEqual(error['network_status'], 'error')
        self.assertNotIn('network_rx', error)
        self.assertIsNone(error['network_rx_bps'])
        self.counters(lo=(999, 999))
        unavailable = self.sample(70)
        self.assertEqual(unavailable['network_status'], 'unavailable')
        self.assertIsNone(unavailable['network_tx_bps'])

    def test_slow_base_collection_does_not_block_cached_network_snapshot(self):
        self.interface('eth0')
        self.counters(eth0=(100, 200))
        collector = node.MetricsCollector()
        collector.network = self.sampler
        entered, release = threading.Event(), threading.Event()

        def slow_metrics(_):
            entered.set()
            release.wait(3)
            return (100, 20), {'cpu_percent': None}

        try:
            with patch.object(node, 'metrics', side_effect=slow_metrics):
                collector.snapshot()
                self.assertTrue(entered.wait(1))
                started = time.monotonic()
                sample = collector.snapshot()
                self.assertLess(time.monotonic() - started, .1)
                self.assertEqual(sample['network_status'], 'collecting')
                self.assertEqual(sample['network_rx'], 100)
        finally:
            collector.stop()
            release.set()
            collector.worker.join(2)


class ProbeTests(unittest.TestCase):
    def test_capacity_units_and_root_disk(self):
        files = {
            '/proc/meminfo': 'MemTotal: 8192 kB\nMemAvailable: 2048 kB\nMemFree: 1000 kB\n',
            '/proc/net/dev': 'header\nheader\n lo: 900 0 0 0 0 0 0 0 800 0 0 0 0 0 0 0\n eth0: 111 0 0 0 0 0 0 0 222 0 0 0 0 0 0 0\n',
            '/proc/uptime': '12345 0\n',
        }
        with patch.object(node, 'cpu', return_value=(200, 70)), \
                patch.object(node.pathlib.Path, 'read_text', lambda path: files[path.as_posix()]), \
                patch.object(node.shutil, 'disk_usage', return_value=SimpleNamespace(total=10000, used=4321)) as disk, \
                patch.object(node.os, 'getloadavg', return_value=(.2, .3, .4), create=True):
            _, value = node.metrics((100, 20))
            _, first = node.metrics(None)
            _, unchanged = node.metrics((200, 70))
        self.assertEqual(disk.call_count, 3)
        self.assertTrue(all(call.args == ('/',) for call in disk.call_args_list))
        self.assertIsNone(first['cpu_percent'])
        self.assertIsNone(unchanged['cpu_percent'])
        self.assertEqual(value['memory_total_bytes'], 8192 * 1024)
        self.assertEqual(value['memory_used_bytes'], 6144 * 1024)
        self.assertEqual(value['memory_percent'], 75)
        self.assertEqual(value['cpu_percent'], 50)
        self.assertEqual((value['disk_used_bytes'], value['disk_total_bytes']), (4321, 10000))

    def test_first_cpu_sample_is_unknown_and_worker_snapshot_keeps_its_timestamp(self):
        collector = node.MetricsCollector()
        with patch.object(node, 'metrics', return_value=((200, 70), {'cpu_percent': None})) as collect, \
                patch.object(node.time, 'time', return_value=1000), \
                patch.object(node.time, 'monotonic', return_value=10):
            collector._update()
            collector.stop()
            first = collector.snapshot()
            first['agent_version'] = 'must-not-change-cache'
            second = collector.snapshot()
        collect.assert_called_once_with(None)
        self.assertIsNone(second['cpu_percent'])
        self.assertEqual(second['metrics_sampled_at'], 1000)
        self.assertEqual(second['metrics_status'], 'ok')
        self.assertNotIn('agent_version', second)
        self.assertEqual(collector.previous, (200, 70))

    def test_resource_worker_stale_and_failed_samples_do_not_repeat_old_values(self):
        collector = node.MetricsCollector()
        collector.stop()
        with patch.object(node, 'metrics', return_value=((200, 70), {'cpu_percent': 50})), \
                patch.object(node.time, 'time', return_value=1000), \
                patch.object(node.time, 'monotonic', return_value=10):
            collector._update()
        with patch.object(node.time, 'monotonic', return_value=46):
            stale = collector.snapshot()
        self.assertEqual(stale['metrics_status'], 'stale')
        self.assertEqual(stale['metrics_sampled_at'], 1000)
        self.assertNotIn('cpu_percent', stale)
        with patch.object(node, 'metrics', side_effect=OSError('slow mount failed')):
            collector._update()
        failed = collector.snapshot()
        self.assertEqual(failed['metrics_status'], 'error')
        self.assertEqual(failed['metrics_sampled_at'], 1000)
        self.assertNotIn('cpu_percent', failed)
        self.assertEqual(collector.previous, (200, 70))

    def test_slow_completed_collection_is_aged_from_start_not_completion(self):
        collector = node.MetricsCollector()
        collector.stop()
        clock = {'wall': 1000, 'monotonic': 10}

        def slow_collection(_):
            # CPU/memory were read, then statvfs took forty seconds to return.
            clock.update(wall=1040, monotonic=50)
            return (200, 70), {'cpu_percent': 50, 'memory_percent': 60}

        with patch.object(node, 'metrics', side_effect=slow_collection), \
                patch.object(node.time, 'time', side_effect=lambda: clock['wall']), \
                patch.object(node.time, 'monotonic', side_effect=lambda: clock['monotonic']):
            collector._update()
            stale = collector.snapshot()
        self.assertEqual(stale['metrics_status'], 'stale')
        self.assertEqual(stale['metrics_sampled_at'], 1000)
        self.assertNotIn('cpu_percent', stale)
        self.assertNotIn('memory_percent', stale)

    def test_resource_worker_is_reused_and_stop_wakes_wait_without_extra_collection(self):
        collector = node.MetricsCollector()
        ready = threading.Event()

        def collect(previous):
            ready.set()
            return (200, 70), {'cpu_percent': None}

        with patch.object(node, 'metrics', side_effect=collect) as metrics:
            collector.snapshot()
            worker = collector.worker
            self.assertTrue(ready.wait(1))
            for _ in range(5):
                collector.snapshot()
                self.assertIs(collector.worker, worker)
            collector.stop()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            metrics.assert_called_once_with(None)

    def test_manual_location_is_not_inferred_and_environment_overrides_saved_config(self):
        cfg = {'hub': 'https://hub.test', 'location': {'country_code': 'de', 'country_name': '德国', 'city': 'Berlin'}}
        result = node.location(cfg, {'FLEET_COUNTRY_CODE': 'jp', 'FLEET_CITY': 'Tokyo'})
        self.assertEqual(result, {'country_code': 'JP', 'city': 'Tokyo'})
        self.assertEqual(node.location(cfg, {'FLEET_COUNTRY_CODE': 'DE'}),
                         {'country_code': 'DE', 'country_name': '德国', 'city': 'Berlin'})
        self.assertEqual(node.location({'hub': 'https://cn.example'}, {}), {})
        self.assertNotIn('country_code', node.location({}, {'FLEET_COUNTRY_CODE': 'invalid'}))

    def test_nvml_is_preferred_and_static_names_are_cached(self):
        nvml = SimpleNamespace(
            nvmlInit=Mock(), nvmlShutdown=Mock(), nvmlDeviceGetCount=Mock(return_value=2),
            nvmlDeviceGetHandleByIndex=lambda index: index,
            nvmlDeviceGetName=Mock(side_effect=[b'Test GPU A', b'Test GPU B']),
            nvmlDeviceGetUtilizationRates=lambda handle: SimpleNamespace(gpu=[0, 80][handle]),
            nvmlDeviceGetMemoryInfo=lambda handle: SimpleNamespace(used=1024, total=4096),
            nvmlDeviceGetTemperature=lambda *args: 56, NVML_TEMPERATURE_GPU=0)
        collector = node.GPUCollector()
        with patch.dict(sys.modules, {'pynvml': nvml}), patch.object(node.shutil, 'which', return_value=None):
            first, second = collector.collect(), collector.collect()
        self.assertEqual(first['gpu_status'], 'ok')
        self.assertEqual(first['gpu_percent'], 40)
        self.assertEqual(second['gpus'][0]['util_percent'], 0)
        self.assertEqual(second['gpus'][1]['memory_total_bytes'], 4096)
        self.assertEqual(nvml.nvmlDeviceGetName.call_count, 2)

    def test_nvidia_csv_fallback_units_missing_data_and_static_cache(self):
        collector = node.GPUCollector()
        replies = ['0, "Fixture GPU, A"\n1, Fixture GPU B\n',
                   '0, 37.5, 2048, 24576, 61\n1, [N/A], [N/A], 24576, [N/A]\n',
                   '0, 40, 2048, 24576, 62\n1, 20, 4096, 24576, 60\n']
        with patch.object(collector, 'nvidia_nvml', side_effect=RuntimeError('NVML library not found')), \
                patch.object(node.shutil, 'which', side_effect=lambda command: command if command == 'nvidia-smi' else None), \
                patch.object(collector, 'command', side_effect=replies) as command:
            first, second = collector.collect(), collector.collect()
        self.assertEqual(first['gpu_percent'], 37.5)
        self.assertIsNone(first['gpus'][1]['util_percent'])
        self.assertIsNone(first['gpus'][1]['memory_used_bytes'])
        self.assertEqual(first['gpus'][0]['name'], 'Fixture GPU, A')
        self.assertEqual(first['gpus'][0]['memory_used_bytes'], 2048 * 1048576)
        self.assertEqual(second['gpu_percent'], 30)
        self.assertEqual(sum('index,name' in ' '.join(call.args[0]) for call in command.call_args_list), 1)

    def test_nvidia_cli_timeout_is_bounded_and_not_reported_as_no_gpu(self):
        collector = node.GPUCollector()
        with patch.object(collector, 'nvidia_nvml', return_value=None), \
                patch.object(node.shutil, 'which', side_effect=lambda name: name if name == 'nvidia-smi' else None), \
                patch.object(node.subprocess, 'run', side_effect=subprocess.TimeoutExpired('nvidia-smi', 3)) as command:
            result = collector.collect()
        self.assertEqual(result['gpu_status'], 'error')
        self.assertIsNone(result['gpu_percent'])
        self.assertEqual(command.call_args.kwargs['timeout'], 3)

    def test_nvml_missing_utilization_falls_back_without_losing_known_device(self):
        for fallback in ([node.gpu_row(0, 'nvidia', 'Fixture', 32)], RuntimeError('CLI failure')):
            collector = node.GPUCollector()
            nvml_rows = [node.gpu_row(0, 'nvidia', 'Fixture', used=1024, total=4096)]
            with patch.object(collector, 'nvidia_nvml', return_value=nvml_rows), \
                    patch.object(node.shutil, 'which', side_effect=lambda name: name if name == 'nvidia-smi' else None), \
                    patch.object(collector, 'nvidia_cli', side_effect=fallback if isinstance(fallback, Exception) else None,
                                 return_value=fallback) as cli:
                result = collector.collect()
            cli.assert_called_once()
            self.assertEqual(len(result['gpus']), 1)
            if isinstance(fallback, Exception):
                self.assertEqual(result['gpus'][0]['memory_total_bytes'], 4096)
                self.assertIsNone(result['gpu_percent'])
            else:
                self.assertEqual(result['gpu_percent'], 32)

    def test_rocm_json_preserves_zero_and_native_byte_counts(self):
        collector = node.GPUCollector()
        response = {'card0': {'GPU use (%)': '0', 'VRAM Total Memory (B)': '21474836480',
                              'VRAM Total Used Memory (B)': '1048576', 'Card Series': 'Fixture AMD',
                              'Temperature (Sensor edge) (C)': '38.5'}}
        with patch.object(collector, 'command', return_value=json.dumps(response)):
            rows = collector.rocm_cli('rocm-smi')
        self.assertEqual(rows[0]['util_percent'], 0)
        self.assertEqual(rows[0]['memory_total_bytes'], 21474836480)
        self.assertEqual(rows[0]['temperature_c'], 38.5)

    def test_amd_smi_json_unit_wrappers_and_unknown_utilization(self):
        collector = node.GPUCollector()
        static = [{'gpu': 0, 'asic': {'market_name': 'Fixture Instinct'}}]
        metric = [{'gpu': 0, 'usage': {'gfx_activity': {'value': 52, 'unit': '%'}},
                   'mem_usage': {'used_vram': {'value': 128, 'unit': 'MB'},
                                 'total_vram': {'value': 65536, 'unit': 'MB'}},
                   'temperature': {'edge': {'value': 48, 'unit': 'C'}}}]
        with patch.object(collector, 'command', side_effect=[json.dumps(static), json.dumps(metric)]):
            rows = collector.amd_cli('amd-smi')
        self.assertEqual(rows[0]['util_percent'], 52)
        self.assertEqual(rows[0]['memory_used_bytes'], 128 * 1048576)
        self.assertEqual(rows[0]['name'], 'Fixture Instinct')
        self.assertIsNone(node.gpu_row(0, 'amd', 'Fixture', 'N/A')['util_percent'])
        self.assertIsNone(node.number('NaN'))
        self.assertIsNone(node.number(float('inf')))

    def test_no_device_driver_and_permission_have_distinct_states(self):
        for status in ('no_gpu', 'driver_missing', 'unsupported'):
            collector = node.GPUCollector()
            with patch.object(collector, 'nvidia_nvml', return_value=None), \
                    patch.object(node.shutil, 'which', return_value=None), \
                    patch.object(collector, 'hardware_status', return_value=(status, 'fixture')):
                result = collector.collect()
            self.assertEqual(result['gpu_status'], status)
            self.assertIsNone(result['gpu_percent'])
        collector = node.GPUCollector()
        with patch.object(collector, 'nvidia_nvml', side_effect=PermissionError('permission denied')), \
                patch.object(node.shutil, 'which', return_value=None):
            self.assertEqual(collector.collect()['gpu_status'], 'permission_denied')

    def test_virtual_vga_is_not_compute_gpu_and_real_devices_still_report(self):
        with tempfile.TemporaryDirectory() as directory:
            pci = Path(directory)
            display = pci / 'display'
            display.mkdir()
            (display / 'class').write_text('0x030000\n')
            for vendor in ('0x1013', '0x1234', '0x1b36', '0x1af4', '0x15ad', '0x1a03', '0x102b'):
                (display / 'vendor').write_text(vendor + '\n')
                self.assertEqual(node.GPUCollector.hardware_status(pci), ('no_gpu', None), vendor)
            accelerator = pci / 'accelerator'
            accelerator.mkdir()
            (accelerator / 'class').write_text('0x030200\n')
            (accelerator / 'vendor').write_text('0x10de\n')
            self.assertEqual(node.GPUCollector.hardware_status(pci)[0], 'driver_missing')
            (accelerator / 'vendor').write_text('0x8086\n')
            self.assertEqual(node.GPUCollector.hardware_status(pci)[0], 'unsupported')
            (accelerator / 'class').write_text('0x120000\n')
            (accelerator / 'vendor').write_text('0x1002\n')
            self.assertEqual(node.GPUCollector.hardware_status(pci)[0], 'driver_missing')

    def test_mixed_vendor_indices_are_unique_and_single_vendor_indices_are_preserved(self):
        collector = node.GPUCollector()
        nvidia = [node.gpu_row(0, 'nvidia', 'Fixture NVIDIA', 20)]
        amd = [node.gpu_row(0, 'amd', 'Fixture AMD', 80)]
        with patch.object(collector, 'nvidia_nvml', return_value=nvidia), \
                patch.object(node.shutil, 'which', side_effect=lambda name: name if name == 'amd-smi' else None), \
                patch.object(collector, 'amd_cli', return_value=amd):
            result = collector.collect()
        self.assertEqual([row['index'] for row in result['gpus']], [0, 1])
        self.assertEqual([row['vendor'] for row in result['gpus']], ['nvidia', 'amd'])
        self.assertEqual(result['gpu_percent'], 50)
        nvidia = [node.gpu_row(index, 'nvidia', 'Fixture', 10) for index in (3, 7)]
        with patch.object(collector, 'nvidia_nvml', return_value=nvidia), \
                patch.object(node.shutil, 'which', return_value=None):
            result = collector.collect()
        self.assertEqual([row['index'] for row in result['gpus']], [3, 7])

    def test_absent_gpu_cache_avoids_repeated_probe(self):
        collector = node.GPUCollector()
        missing = {'gpus': [], 'gpu_percent': None, 'gpu_status': 'no_gpu', 'gpu_error': None}
        with patch.object(node.time, 'monotonic', return_value=100), \
                patch.object(collector, 'collect', return_value=missing) as collect:
            collector._update()
            self.assertEqual(collector.snapshot(), missing)
            self.assertEqual(collector.snapshot(), missing)
        collect.assert_called_once()
        self.assertEqual(collector.next_probe, 400)

    def test_hung_gpu_worker_cannot_block_heartbeat_or_spawn_unbounded_workers(self):
        collector = node.GPUCollector()
        ready, finish = threading.Event(), threading.Event()
        def blocked():
            ready.set()
            finish.wait(2)
            return {'gpus': [], 'gpu_percent': None, 'gpu_status': 'no_gpu', 'gpu_error': None}
        with patch.object(collector, 'collect', side_effect=blocked) as collect:
            started = time.monotonic()
            collector.snapshot()
            self.assertLess(time.monotonic() - started, .25)
            self.assertTrue(ready.wait(1))
            collector.snapshot()
            collect.assert_called_once()
            finish.set()
            collector.worker.join(2)
        self.assertFalse(collector.worker.is_alive())

    def test_heartbeat_failed_samples_retry_and_success_rtt_is_sent_next_time(self):
        queue = node.HeartbeatLatency()
        cfg = {'hub': 'https://hub.test', 'id': 'fixture', 'token': 'synthetic'}
        attempts = []
        def transport(*args):
            attempts.append(copy.deepcopy(args[3]))
            if len(attempts) == 1:
                raise TimeoutError('synthetic timeout')
            return {'ok': True}
        with patch.object(node, 'request', side_effect=transport), \
                patch.object(node.time, 'monotonic', side_effect=[1, 21, 30, 30.125, 40, 40.250]), \
                patch.object(node.time, 'time', side_effect=[1000, 1010, 1020]):
            with self.assertRaises(TimeoutError): queue.send(cfg, {})
            queue.send(cfg, {})
            queue.send(cfg, {})
        self.assertEqual(attempts[0]['latency_samples'], [])
        self.assertEqual(attempts[1]['latency_samples'], [{'timestamp': 1000, 'success': False, 'latency_ms': None}])
        self.assertFalse(attempts[1]['latency_success'])
        self.assertEqual(attempts[2]['latency_samples'], [{'timestamp': 1010, 'success': True, 'latency_ms': 125}])
        self.assertEqual(attempts[2]['latency_ms'], 125)
        self.assertEqual(list(queue.samples), [{'timestamp': 1020, 'success': True, 'latency_ms': 250}])

    def test_failed_heartbeat_queue_is_bounded_and_never_zero_latency(self):
        queue = node.HeartbeatLatency()
        cfg = {'hub': 'https://hub.test', 'id': 'fixture', 'token': 'synthetic'}
        with patch.object(node, 'request', side_effect=OSError('offline')), \
                patch.object(node.time, 'monotonic', return_value=10), \
                patch.object(node.time, 'time', side_effect=range(node.LATENCY_QUEUE_LIMIT + 5)):
            for _ in range(node.LATENCY_QUEUE_LIMIT + 5):
                with self.assertRaises(OSError): queue.send(cfg, {})
        self.assertEqual(len(queue.samples), node.LATENCY_QUEUE_LIMIT)
        self.assertEqual(queue.samples[0]['timestamp'], 5)
        self.assertTrue(all(not row['success'] and row['latency_ms'] is None for row in queue.samples))


if __name__ == '__main__':
    unittest.main(verbosity=2)
