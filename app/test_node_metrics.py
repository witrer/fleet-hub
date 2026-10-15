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


class ProbeTests(unittest.TestCase):
    def test_capacity_units_root_disk_and_loopback_exclusion(self):
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
        disk.assert_called_once_with('/')
        self.assertEqual(value['memory_total_bytes'], 8192 * 1024)
        self.assertEqual(value['memory_used_bytes'], 6144 * 1024)
        self.assertEqual(value['memory_percent'], 75)
        self.assertEqual(value['cpu_percent'], 50)
        self.assertEqual((value['disk_used_bytes'], value['disk_total_bytes']), (4321, 10000))
        self.assertEqual((value['network_rx'], value['network_tx']), (111, 222))

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
