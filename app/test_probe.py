import importlib.util
import json
import math
from pathlib import Path
import sqlite3
import unittest

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
