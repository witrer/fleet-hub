import importlib.util
import json
import math
from pathlib import Path
import sqlite3
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('probe_next', Path(__file__).with_name('probe.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
NOW = 1700010321


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.store = probe.ProbeStore(self.db)

    def tearDown(self):
        self.db.close()

    def test_legacy_payload_optional_validation_and_no_gpu(self):
        clean = self.store.clean_metrics({'cpu_percent': 23, 'platform': 'Linux x86_64', 'uptime': 50})
        self.assertEqual(clean['cpu_percent'], 23)
        self.assertNotIn('gpus', clean)
        invalid = self.store.clean_metrics({'memory_percent': True, 'disk_percent': float('nan'),
                                           'memory_total_bytes': -2, 'network_rx': 10**1000,
                                           'gpus': [], 'gpu_percent': 0, 'country_code': 'china'})
        self.assertIsNone(invalid['memory_percent'])
        self.assertIsNone(invalid['disk_percent'])
        self.assertIsNone(invalid['network_rx'])
        self.assertIsNone(invalid['gpu_percent'])
        self.assertEqual(invalid['country_code'], '')
        enriched = self.store.enrich({'id': 'old', 'last_seen': NOW - 5}, NOW)
        self.assertIsNone(enriched['gpu_percent'])
        self.assertEqual(enriched['freshness']['status'], 'fresh')
        self.assertEqual(enriched['gpus'], [])

    def test_history_has_null_gaps_and_weighted_actual_stats(self):
        first = NOW // 60 * 60 - 120
        self.store.record('n', {'cpu_percent': 10}, first + 1)
        self.store.record('n', {'cpu_percent': 90}, first + 10)
        self.store.record('n', {'cpu_percent': 30}, first + 121)
        history = self.store.history('n', 'cpu', now=NOW)
        values = {point['timestamp']: point['value'] for point in history['points']}
        self.assertEqual(len(values), 60)
        self.assertEqual(values[first], 50)
        self.assertIsNone(values[first + 60])
        self.assertEqual(values[first + 120], 30)
        self.assertEqual(history['stats']['min'], 10)
        self.assertEqual(history['stats']['max'], 90)
        self.assertAlmostEqual(history['stats']['avg'], 130 / 3)

    def test_network_values_preserve_units_and_reject_invalid_optional_fields(self):
        clean = self.store.clean_metrics({'network_status': 'ok', 'network_rx_bps': 123456.75,
            'network_tx_bps': None, 'network_rx': 9000000, 'network_tx_errors': 4,
            'network_rx_dropped': 5, 'network_interfaces': ['eth0', 'eth0', '../bad', 1, 'wg0'],
            'network_scope': 'default_route', 'network_counter_scope': 'interface_lifetime',
            'network_sampled_at': NOW, 'network_interval_seconds': 10})
        self.assertEqual(clean['network_rx_bps'], 123456.75)
        self.assertIsNone(clean['network_tx_bps'])
        self.assertEqual(clean['network_rx'], 9000000)
        self.assertEqual(clean['network_interfaces'], ['eth0', 'wg0'])
        self.assertEqual(clean['network_tx_errors'], 4)
        self.assertEqual(clean['network_rx_dropped'], 5)
        self.assertNotIn('network_rx_bps', self.store.clean_metrics({'network_rx': 100}))
        for invalid in (True, -1, float('nan'), float('inf'), 10 ** 1000, '50'):
            with self.subTest(value=invalid):
                self.assertIsNone(self.store.clean_metrics({'network_rx_bps': invalid})['network_rx_bps'])

    def test_network_history_uses_its_timestamp_and_deduplicates_after_restart(self):
        sampled = NOW // 60 * 60 - 2
        data = {'metrics_status': 'error', 'network_status': 'ok', 'network_sampled_at': sampled,
                'network_rx_bps': 200000, 'network_tx_bps': 0}
        self.store.record('n', data, NOW, record_resource_sample=False)
        reopened = probe.ProbeStore(self.db)
        reopened.record('n', data, NOW + 1, record_resource_sample=False)
        updated = dict(data, network_sampled_at=sampled + 1, network_rx_bps=400000, network_tx_bps=None)
        reopened.record('n', updated, NOW + 2, record_resource_sample=False)
        # Replaying an earlier cached sample cannot change an aggregate either.
        reopened.record('n', data, NOW + 3, record_resource_sample=False)
        history = reopened.history('n', 'network_rx', now=NOW + 3)
        self.assertEqual(history['unit'], 'bytes_per_second')
        self.assertEqual(history['points'][-2]['value'], 300000)
        self.assertIsNone(history['points'][-1]['value'])
        self.assertEqual(history['stats'], {'min': 200000, 'max': 400000, 'avg': 300000})
        tx = reopened.history('n', 'network_tx', now=NOW + 3)
        self.assertEqual(tx['stats']['avg'], 0)
        resource = json.loads(self.db.execute('SELECT resources FROM probe_minutes WHERE node_id=? AND minute=?',
                                              ('n', int(sampled) // 60 * 60)).fetchone()[0])
        self.assertEqual(resource['network_rx']['count'], 2)
        self.assertEqual(resource['network_tx']['count'], 1)
        self.assertEqual(reopened.history('n', 'cpu', now=NOW)['unit'], 'percent')
        with self.assertRaises(ValueError):
            reopened.history('n', 'network_rx', device=0, now=NOW)

    def test_unknown_reset_and_stale_network_samples_do_not_record_zero_or_rate(self):
        for status in ('collecting', 'reset', 'unavailable', 'error', 'stale'):
            with self.subTest(status=status):
                data = {'network_status': status, 'network_sampled_at': NOW, 'network_rx_bps': 1000}
                self.assertIsNone(self.store.clean_metrics(data)['network_rx_bps'])
                self.store.record(status, data, NOW)
                self.assertIsNone(self.store.history(status, 'network_rx', now=NOW)['stats']['avg'])
        for timestamp in (NOW - 35, NOW + 61, None):
            with self.subTest(timestamp=timestamp):
                self.store.record('old', {'network_status': 'ok', 'network_sampled_at': timestamp,
                                          'network_rx_bps': 1000}, NOW)
        self.assertIsNone(self.store.history('old', 'network_rx', now=NOW)['stats']['avg'])

    def test_network_freshness_is_independent_of_heartbeat_and_basic_metrics(self):
        fresh = self.store.enrich({'id': 'n', 'last_seen': NOW, 'metrics_status': 'error',
            'network_status': 'ok', 'network_sampled_at': NOW - 1, 'network_rx_bps': 1024,
            'network_tx_bps': 2048, 'network_rx': 99999}, NOW)
        self.assertEqual(fresh['network_rx_bps'], 1024)
        stale = self.store.enrich(dict(fresh, network_sampled_at=NOW - 35), NOW)
        self.assertEqual(stale['network_status'], 'stale')
        self.assertIsNone(stale['network_rx_bps'])
        self.assertEqual(stale['network_rx'], 99999)
        reset = self.store.enrich(dict(fresh, network_status='reset'), NOW)
        self.assertIsNone(reset['network_tx_bps'])

    def test_collection_metadata_validation_preserves_absence_and_nulls(self):
        self.assertNotIn('metrics_status', self.store.clean_metrics({'cpu_percent': 10}))
        for status in ('collecting', 'ok', 'stale', 'error'):
            with self.subTest(status=status):
                clean = self.store.clean_metrics({'metrics_status': status, 'metrics_sampled_at': NOW,
                                                  'metrics_error': None})
                self.assertEqual(clean, {'metrics_status': status, 'metrics_sampled_at': NOW,
                                        'metrics_error': None})
        for value in (None, True, [], 'unsupported', 10):
            with self.subTest(status=value):
                self.assertEqual(self.store.clean_metrics({'metrics_status': value})['metrics_status'], 'error')
        for value in (None, True, -1, float('nan'), float('inf'), 10**1000, '1700010321'):
            with self.subTest(timestamp=value):
                self.assertIsNone(self.store.clean_metrics({'metrics_sampled_at': value})['metrics_sampled_at'])
        self.assertEqual(self.store.clean_metrics({'metrics_sampled_at': 0})['metrics_sampled_at'], 0)
        self.assertEqual(len(self.store.clean_metrics({'metrics_error': 'e' * 300})['metrics_error']), 240)
        self.assertIsNone(self.store.clean_metrics({'metrics_error': {'message': 'bad'}})['metrics_error'])

    def test_resource_freshness_uses_actual_sampling_not_recent_heartbeat(self):
        for status, stamp, expected, age in (
                ('ok', NOW - 2, 'fresh', 2),
                ('ok', NOW - 35, 'stale', 35),
                ('stale', NOW - 1, 'stale', 1),
                ('collecting', None, 'unknown', None),
                ('error', None, 'unknown', None),
                ('error', NOW - 1, 'stale', 1),
                ('ok', None, 'unknown', None),
                ('ok', NOW + 60, 'fresh', 0),
                ('ok', NOW + 61, 'stale', 0)):
            with self.subTest(status=status, stamp=stamp):
                node = self.store.enrich({'id': 'n', 'last_seen': NOW, 'metrics_status': status,
                                          'metrics_sampled_at': stamp}, NOW)
                self.assertEqual(node['freshness'], {'status': expected, 'age_seconds': age, 'sampled_at': stamp})
                self.assertEqual(node['metrics_stale'], expected != 'fresh')
                self.assertEqual(node['metrics_age_seconds'], age)
                self.assertEqual(node['data_freshness'], expected)
                self.assertEqual(node['heartbeat_freshness'], {'status': 'fresh', 'age_seconds': 0,
                                                               'sampled_at': NOW})
        # Partial new metadata must not fall back to treating a heartbeat as a
        # successful resource observation; old agents keep the old contract.
        partial = self.store.enrich({'id': 'n', 'last_seen': NOW, 'metrics_status': 'collecting'}, NOW)
        self.assertEqual(partial['freshness']['status'], 'unknown')
        partial = self.store.enrich({'id': 'n', 'last_seen': NOW, 'metrics_sampled_at': NOW}, NOW)
        self.assertEqual(partial['freshness']['status'], 'stale')
        legacy = self.store.enrich({'id': 'n', 'last_seen': NOW - 3}, NOW)
        self.assertEqual(legacy['freshness'], legacy['heartbeat_freshness'])
        self.assertEqual(legacy['metrics_age_seconds'], 3)

    def test_repeat_resource_sample_keeps_independent_gpu_and_rtt_history(self):
        metrics = {'cpu_percent': 10, 'memory_percent': 20, 'disk_percent': 30,
                   'metrics_status': 'ok', 'metrics_sampled_at': NOW - 2,
                   'gpus': [{'index': 0, 'util_percent': 20}],
                   'latency_ms': 10, 'latency_sample_at': NOW - 1}
        self.store.record('n', metrics, NOW)
        metrics.update(gpus=[{'index': 0, 'util_percent': 80}], latency_ms=30, latency_sample_at=NOW)
        self.store.record('n', metrics, NOW + 1, record_resource_sample=False)
        resources = json.loads(self.db.execute('SELECT resources FROM probe_minutes WHERE node_id=? AND minute=?',
                                                ('n', NOW // 60 * 60)).fetchone()[0])
        for metric, value in (('cpu', 10), ('memory', 20), ('disk', 30)):
            self.assertEqual(resources[metric], {'count': 1, 'sum': value, 'min': value, 'max': value})
        self.assertEqual(resources['gpu']['count'], 2)
        self.assertEqual(resources['gpu:0']['count'], 2)
        self.assertEqual(self.store.history('n', 'gpu', now=NOW + 1)['stats']['avg'], 50)
        summary = self.store.latency_summary(['n'], now=NOW + 1)['nodes']['n']
        self.assertEqual(sum(bucket['sample_count'] for bucket in summary['buckets']), 2)
        self.assertEqual(summary['avg_ms'], 20)

    def test_prefetched_location_avoids_per_node_metadata_queries(self):
        self.store.set_location('n', {'country_code': 'JP', 'city': 'Tokyo'})
        statements = []
        self.db.set_trace_callback(statements.append)
        overridden = self.store.enrich({'id': 'n', 'country_code': 'US'}, NOW,
                                       manual_location=json.dumps({'country_code': 'CN', 'city': 'Shanghai'}))
        direct = self.store.enrich({'id': 'n'}, NOW, manual_location={'country_code': 'KR', 'city': 'Seoul'})
        no_override = self.store.enrich({'id': 'n', 'country_code': 'US'}, NOW, manual_location=None)
        self.db.set_trace_callback(None)
        self.assertEqual(statements, [])
        self.assertEqual(overridden['country_code'], 'CN')
        self.assertEqual(overridden['location_source'], 'manual')
        self.assertEqual(direct['country_code'], 'KR')
        self.assertEqual(no_override['country_code'], 'US')
        self.assertEqual(no_override['location_source'], 'node')
        self.assertEqual(self.store.enrich({'id': 'n'}, NOW)['country_code'], 'JP')

    def test_exact_p95_dedup_and_strict_utc_buckets(self):
        start = NOW // 60 * 60 - 60
        samples = [{'timestamp': start + i, 'success': True, 'latency_ms': 10 if i < 18 else 1000} for i in range(20)]
        self.store.record('n', {'latency_samples': samples}, NOW)
        self.store.record('n', {'latency_samples': samples}, NOW + 1)
        result = self.store.latency_summary(['n', 'new'], now=NOW + 1)
        buckets = result['nodes']['n']['buckets']
        self.assertEqual(len(buckets), 96)
        self.assertTrue(all(b['start'] % 900 == 0 and b['end'] - b['start'] == 900 for b in buckets))
        self.assertTrue(all(left['end'] == right['start'] for left, right in zip(buckets, buckets[1:])))
        actual = next(b for b in buckets if b['sample_count'])
        self.assertEqual(actual['sample_count'], 20)
        self.assertEqual(actual['success_count'], 20)
        self.assertEqual(actual['p95_ms'], 1000)
        self.assertEqual(actual['avg_ms'], 109)
        self.assertEqual(actual['status'], 'critical')
        self.assertEqual(result['nodes']['n']['p95_ms'], 1000)
        self.assertTrue(all(b['status'] == 'unknown' and b['avg_ms'] is None for b in result['nodes']['new']['buckets']))

    def test_failed_requests_are_critical_not_zero_latency(self):
        self.store.record('n', {'latency_samples': [{'timestamp': NOW - 1, 'success': False, 'latency_ms': 500}]}, NOW)
        bucket = self.store.latency_summary(['n'], now=NOW)['nodes']['n']['buckets'][-1]
        self.assertEqual(bucket['status'], 'critical')
        self.assertEqual(bucket['sample_count'], 1)
        self.assertEqual(bucket['success_count'], 0)
        self.assertIsNone(bucket['avg_ms'])
        self.assertIsNone(bucket['p95_ms'])

    def test_offline_needs_consecutive_evidence_and_never_backfills_hub_outage(self):
        nodes = [{'id': 'n', 'last_seen': NOW - 3600, 'frp_status': None}]
        self.store.mark_offline(nodes, NOW)
        self.assertEqual(self.store.latency_summary(['n'], now=NOW)['nodes']['n']['buckets'][-1]['status'], 'unknown')
        self.store.mark_offline(nodes, NOW + 5)
        self.assertEqual(self.store.latency_summary(['n'], now=NOW + 5)['nodes']['n']['buckets'][-1]['status'], 'offline')
        self.store.mark_offline(nodes, NOW + 3600)
        summary = self.store.latency_summary(['n'], now=NOW + 3600)['nodes']['n']['buckets']
        self.assertEqual(summary[-1]['status'], 'unknown')
        self.assertEqual(summary[-2]['status'], 'unknown')
        self.store.mark_offline(nodes, NOW + 3605)
        self.assertEqual(self.store.latency_summary(['n'], now=NOW + 3605)['nodes']['n']['buckets'][-1]['status'], 'offline')
        restarted = probe.ProbeStore(self.db)
        restarted.mark_offline(nodes, NOW + 7200)
        self.assertEqual(restarted.latency_summary(['n'], now=NOW + 7200)['nodes']['n']['buckets'][-1]['status'], 'unknown')

    def test_unknown_proxy_state_is_not_explicit_offline(self):
        nodes = [{'id': 'n', 'last_seen': 0, 'online': False, 'frp_status': None}]
        self.store.mark_offline(nodes, NOW)
        self.store.mark_offline(nodes, NOW + 5)
        self.assertEqual(self.store.latency_summary(['n'], now=NOW + 5)['nodes']['n']['buckets'][-1]['status'], 'unknown')
        nodes[0]['frp_status'] = 'offline'
        self.store.mark_offline(nodes, NOW + 10)
        self.store.mark_offline(nodes, NOW + 15)
        self.assertEqual(self.store.latency_summary(['n'], now=NOW + 15)['nodes']['n']['buckets'][-1]['status'], 'offline')

    def test_failed_request_takes_priority_over_offline_observations(self):
        nodes = [{'id': 'n', 'last_seen': NOW - 100, 'frp_status': 'offline'}]
        self.store.mark_offline(nodes, NOW - 5)
        self.store.mark_offline(nodes, NOW)
        self.store.record('n', {'latency_samples': [{'timestamp': NOW - 1, 'success': False, 'latency_ms': None}]}, NOW)
        bucket = self.store.latency_summary(['n'], now=NOW)['nodes']['n']['buckets'][-1]
        self.assertEqual(bucket['status'], 'critical')
        self.assertEqual(bucket['reason'], 'heartbeat_request_failed')
        self.assertEqual(bucket['sample_count'], 1)
        self.assertIsNone(bucket['avg_ms'])

    def test_manual_location_overrides_node_and_freshness_stales(self):
        self.store.set_location('n', {'country_code': 'cn', 'country_name': '中国', 'city': '上海', 'region': ''})
        state = self.store.enrich({'id': 'n', 'last_seen': NOW - 40, 'country_code': 'US', 'country_name': '美国'}, NOW)
        self.assertEqual(state['country_code'], 'CN')
        self.assertEqual(state['country_name'], '中国')
        self.assertEqual(state['location_source'], 'manual')
        self.assertEqual(state['freshness']['status'], 'stale')
        self.assertTrue(state['metrics_stale'])
        self.store.set_location('n', {})
        self.assertEqual(self.store.enrich(state, NOW)['country_code'], '')

    def test_gpu_average_and_per_device_history(self):
        gpus = [{'index': 0, 'vendor': 'nvidia', 'name': 'A', 'util_percent': 20,
                 'memory_used_bytes': 5, 'memory_total_bytes': 10},
                {'index': 1, 'vendor': 'amd', 'name': 'B', 'util_percent': 80},
                {'index': 2, 'vendor': 'amd', 'name': 'C', 'util_percent': None}]
        clean = self.store.clean_metrics({'gpus': gpus})
        self.assertEqual(clean['gpu_percent'], 50)
        self.store.record('n', clean, NOW)
        self.assertEqual(self.store.history('n', 'gpu', now=NOW)['points'][-1]['value'], 50)
        self.assertEqual(self.store.history('n', 'gpu', device=1, now=NOW)['points'][-1]['value'], 80)
        self.assertIsNone(self.store.history('n', 'gpu', device=2, now=NOW)['points'][-1]['value'])
        self.assertEqual(len(self.store.history('n', 'gpu', now=NOW)['gpus']), 3)

    def test_retention_batch_query_and_compatibility_latency_fields(self):
        self.store.record('n', {'cpu_percent': 80}, NOW - probe.RETENTION - 600)
        self.store.record('n', {'latency_ms': 40, 'latency_sample_at': NOW - 1, 'latency_success': True}, NOW)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM probe_minutes WHERE minute<?', (NOW - probe.RETENTION,)).fetchone()[0], 0)
        statements = []
        self.db.set_trace_callback(statements.append)
        result = self.store.latency_summary(['n', 'x', 'y'], now=NOW)
        self.db.set_trace_callback(None)
        self.assertEqual(len([query for query in statements if query.startswith('SELECT')]), 1)
        self.assertEqual(result['nodes']['n']['avg_ms'], 40)
        self.assertEqual(result['nodes']['n']['buckets'][-1]['sample_count'], 1)

    def test_cached_summary_avoids_repeat_queries_and_preserves_generation_time(self):
        self.store.record('n', {'latency_ms': 40, 'latency_sample_at': NOW - 1}, NOW)
        statements = []
        self.db.set_trace_callback(statements.append)
        with mock.patch.object(probe.time, 'monotonic', return_value=100):
            first = self.store.cached_latency_summary(['n'], now=NOW)
        self.store.record('n', {'latency_ms': 200, 'latency_sample_at': NOW + 1}, NOW + 1)
        with mock.patch.object(probe.time, 'monotonic', return_value=114):
            cached = self.store.cached_latency_summary(['n', 'n'], now=NOW + 14)
        self.db.set_trace_callback(None)
        self.assertIs(cached, first)
        self.assertEqual(cached['generated_at'], NOW)
        summary_queries = [query for query in statements if query.startswith('SELECT node_id,minute,latencies')]
        self.assertEqual(len(summary_queries), 1)
        self.assertEqual(cached['nodes']['n']['avg_ms'], 40)
        self.assertEqual(self.store.latency_summary(['n'], now=NOW + 14)['nodes']['n']['avg_ms'], 120)

    def test_cached_summary_expires_on_monotonic_deadline(self):
        with mock.patch.object(probe.time, 'monotonic', return_value=100):
            first = self.store.cached_latency_summary(['n'], now=NOW)
        self.store.record('n', {'latency_ms': 40, 'latency_sample_at': NOW + 1}, NOW + 1)
        # TTL still expires even if the wall clock has barely moved.
        with mock.patch.object(probe.time, 'monotonic', return_value=115):
            refreshed = self.store.cached_latency_summary(['n'], now=NOW + 1)
        self.assertIsNot(refreshed, first)
        self.assertEqual(refreshed['generated_at'], NOW + 1)
        self.assertEqual(refreshed['nodes']['n']['avg_ms'], 40)

    def test_cached_summary_separates_thresholds_and_rejects_invalid_aliases(self):
        self.store.record('n', {'latency_ms': 150, 'latency_sample_at': NOW - 1}, NOW)
        with mock.patch.object(probe.time, 'monotonic', return_value=100):
            normal = self.store.cached_latency_summary(['n'], now=NOW)
            custom = self.store.cached_latency_summary(['n'], healthy_ms=200, now=NOW)
            self.store.cached_latency_summary(['n'], healthy_ms=1, critical_ms=250, now=NOW)
            for healthy, critical in [(True, 250), (0, False), (250, 100), ('1', 250),
                                      (float('nan'), 250), (100, float('inf'))]:
                with self.subTest(healthy=healthy, critical=critical), self.assertRaises(ValueError):
                    self.store.cached_latency_summary(['n'], healthy_ms=healthy, critical_ms=critical, now=NOW)
        self.assertEqual(normal['nodes']['n']['buckets'][-1]['status'], 'degraded')
        self.assertEqual(custom['nodes']['n']['buckets'][-1]['status'], 'healthy')

    def test_cached_summary_immediately_excludes_revoked_nodes(self):
        with mock.patch.object(probe.time, 'monotonic', return_value=100):
            original = self.store.cached_latency_summary(['n', 'revoked'], now=NOW)
            current = self.store.cached_latency_summary(['n'], now=NOW + 1)
            empty = self.store.cached_latency_summary([], now=NOW + 2)
        self.assertEqual(set(original['nodes']), {'n', 'revoked'})
        self.assertEqual(set(current['nodes']), {'n'})
        self.assertEqual(current['generated_at'], NOW + 1)
        self.assertEqual(empty['nodes'], {})

    def test_cached_summary_refreshes_at_utc_bucket_boundary(self):
        boundary = NOW // 900 * 900 + 900
        with mock.patch.object(probe.time, 'monotonic', return_value=100):
            before = self.store.cached_latency_summary(['n'], now=boundary - 1)
            after = self.store.cached_latency_summary(['n'], now=boundary)
        self.assertEqual(after['generated_at'], boundary)
        self.assertEqual(after['nodes']['n']['buckets'][-1]['start'], boundary)
        self.assertEqual(before['nodes']['n']['buckets'][-1]['end'], boundary)

    def test_cached_summary_never_returns_future_generation_after_clock_rollback(self):
        with mock.patch.object(probe.time, 'monotonic', return_value=100):
            later = self.store.cached_latency_summary(['n'], now=NOW)
            earlier = self.store.cached_latency_summary(['n'], now=NOW - 1)
        self.assertIsNot(later, earlier)
        self.assertEqual(earlier['generated_at'], NOW - 1)

    def test_cached_summary_bounds_memory_and_evicts_least_recently_used(self):
        with mock.patch.object(probe.time, 'monotonic', return_value=100):
            originals = [self.store.cached_latency_summary(['n'], healthy_ms=value, now=NOW)
                         for value in range(4)]
            self.assertIs(self.store.cached_latency_summary(['n'], healthy_ms=0, now=NOW), originals[0])
            self.store.cached_latency_summary(['n'], healthy_ms=4, now=NOW)
            self.assertEqual(len(self.store._latency_summary_cache), probe.LATENCY_SUMMARY_CACHE_LIMIT)
            self.assertIs(self.store.cached_latency_summary(['n'], healthy_ms=0, now=NOW), originals[0])
            self.assertIsNot(self.store.cached_latency_summary(['n'], healthy_ms=1, now=NOW), originals[1])
            self.assertEqual(len(self.store._latency_summary_cache), probe.LATENCY_SUMMARY_CACHE_LIMIT)


def test_sqlite_disk_reopen_preserves_resource_rtt_and_manual_location(tmp_path):
    path = tmp_path / 'probe.db'
    first = sqlite3.connect(path)
    store = probe.ProbeStore(first)
    store.record('persistent-node', {'cpu_percent': 37, 'memory_percent': 62,
                 'latency_samples': [{'timestamp': NOW - 2, 'success': True, 'latency_ms': 40},
                                     {'timestamp': NOW - 1, 'success': True, 'latency_ms': 200}]}, NOW)
    store.set_location('persistent-node', {'country_code': 'JP', 'country_name': '日本', 'region': '', 'city': '东京'})
    first.close()

    second = sqlite3.connect(path)
    try:
        reopened = probe.ProbeStore(second)
        history = reopened.history('persistent-node', 'cpu', now=NOW)
        assert history['points'][-1]['value'] == 37
        assert history['stats'] == {'min': 37, 'max': 37, 'avg': 37}
        summary = reopened.latency_summary(['persistent-node'], now=NOW)['nodes']['persistent-node']
        assert summary['avg_ms'] == 120 and summary['p95_ms'] == 200
        assert sum(bucket['sample_count'] for bucket in summary['buckets']) == 2
        node = reopened.enrich({'id': 'persistent-node', 'last_seen': NOW, 'country_code': 'US'}, NOW)
        assert node['country_code'] == 'JP' and node['city'] == '东京'
        assert node['location_source'] == 'manual'
        reopened.mark_offline([{'id': 'persistent-node', 'last_seen': NOW, 'frp_status': None}], NOW + 1800)
        latest = reopened.latency_summary(['persistent-node'], now=NOW + 1800)['nodes']['persistent-node']['buckets'][-1]
        assert latest['status'] == 'unknown'
    finally:
        second.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
