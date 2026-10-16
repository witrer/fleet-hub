"""HTTP contracts for the integrated probe API, with unrelated OAuth installation isolated."""
import importlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi.testclient import TestClient

NOW = 1700010321
NODE = 'node-0000000001'
OTHER = 'node-0000000002'
REVOKED = 'node-0000000003'
ADMIN = {'Authorization': 'Bearer test-probe-admin'}
NODE_AUTH = {'Authorization': 'Bearer test-probe-node'}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    default = Path(__file__).with_name('app.py')
    if not default.exists():
        default = Path(__file__).parent / 'fleet-repo/app/app.py'
    target = Path(os.environ.get('FLEET_PROBE_APP', default)).resolve()
    monkeypatch.syspath_prepend(str(target.parent))
    oauth = importlib.import_module('fleet_oauth')
    def install_oauth_stub(app, cfg, root):
        app.state.oauth_provider = SimpleNamespace(base=cfg['base_url'])
    monkeypatch.setattr(oauth, 'install_oauth', install_oauth_stub)
    for name in ('keys', '.ssh'):
        (tmp_path / name).mkdir()
    (tmp_path / 'config.json').write_text(json.dumps({
        'base_url': 'https://hub.test', 'host': 'hub.test', 'admin_token': 'test-probe-admin',
        'frp_dashboard_password': 'test-only', 'assets': {},
    }))
    monkeypatch.setenv('FLEET_ROOT', str(tmp_path))
    spec = importlib.util.spec_from_file_location('probe_api_contract_app', target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.time, 'time', lambda: NOW)
    for index, node_id in enumerate((NODE, OTHER, REVOKED)):
        module.db.execute('INSERT INTO nodes(id,name,user,port,token_hash,host_key,secret,active) VALUES(?,?,?,?,?,?,?,?)',
                          (node_id, 'Test ' + str(index), 'root', 24000 + index,
                           module.digest('test-probe-node' if node_id == NODE else 'other-node'),
                           'synthetic-public-key', 'test-only', int(node_id != REVOKED)))
    module.db.commit()
    client = TestClient(module.app, base_url='https://hub.test')
    yield module, client
    client.close()
    module.db.close()


@pytest.mark.parametrize('headers', [{}, {'Authorization': 'Bearer wrong'}, NODE_AUTH])
def test_probe_read_and_manual_location_are_admin_only(hub, headers):
    _, client = hub
    assert client.get('/api/nodes/' + NODE + '/history', headers=headers).status_code == 401
    assert client.get('/api/latency/summary', headers=headers).status_code == 401
    assert client.put('/api/nodes/' + NODE + '/location', headers=headers, json={}).status_code == 401


def test_node_credentials_are_bound_to_the_registered_node(hub):
    _, client = hub
    path = '/api/nodes/' + NODE + '/heartbeat'
    assert client.post(path, json={'cpu_percent': 5}).status_code == 401
    assert client.post('/api/nodes/' + OTHER + '/heartbeat', headers=NODE_AUTH, json={}).status_code == 401
    assert client.post(path, headers=NODE_AUTH, json={'cpu_percent': 5}).status_code == 200


def test_agent_update_observation_only_exposes_display_fields(hub):
    _, client = hub
    update = {'status': 'rolled_back', 'version': 'test-version', 'checked_at': NOW-20,
              'updated_at': NOW-10, 'error': 'startup_timeout',
              'private_path': '/do-not-expose', 'rejected': ['internal-detail'], 'token': 'test-only'}
    assert client.post('/api/nodes/' + NODE + '/heartbeat', headers=NODE_AUTH,
                       json={'agent_version': 'previous', 'agent_update': update}).status_code == 200
    state = client.get('/api/state', headers=ADMIN).json()
    node = next(item for item in state['nodes'] if item['id'] == NODE)
    assert node['agent_version'] == 'previous'
    assert node['agent_update'] == {key: update[key] for key in
                                   ('status', 'version', 'checked_at', 'updated_at', 'error')}


def test_network_monitoring_keeps_real_rates_and_own_sampling_time(hub):
    module, client = hub
    payload = {'metrics_status': 'error', 'network_status': 'ok', 'network_sampled_at': NOW - 1,
               'network_rx_bps': 2097152, 'network_tx_bps': 524288, 'network_rx': 123456789,
               'network_tx': 987654321, 'network_rx_errors': 2, 'network_tx_dropped': 3,
               'network_interfaces': ['ens3'], 'network_scope': 'default_route',
               'network_counter_scope': 'interface_lifetime', 'network_interval_seconds': 10}
    path = '/api/nodes/' + NODE + '/heartbeat'
    assert client.post(path, headers=NODE_AUTH, json=payload).status_code == 200
    assert client.post(path, headers=NODE_AUTH, json=payload).status_code == 200
    state = client.get('/api/state', headers=ADMIN).json()
    node = next(value for value in state['nodes'] if value['id'] == NODE)
    assert node['network_rx_bps'] == 2097152
    assert node['network_interfaces'] == ['ens3']
    assert node['network_rx_errors'] == 2 and node['network_tx_dropped'] == 3
    history = client.get('/api/nodes/' + NODE + '/history?metric=network_rx', headers=ADMIN).json()
    assert history['unit'] == 'bytes_per_second'
    assert history['points'][-1]['value'] == 2097152
    row = module.db.execute('SELECT resources FROM probe_minutes WHERE node_id=? AND minute=?',
                            (NODE, NOW // 60 * 60)).fetchone()
    assert json.loads(row['resources'])['network_rx']['count'] == 1
    assert client.get('/api/nodes/' + NODE + '/history?metric=network_tx&device=0', headers=ADMIN).status_code == 400
    stale = dict(payload, network_sampled_at=NOW - 36)
    assert client.post(path, headers=NODE_AUTH, json=stale).status_code == 200
    state = client.get('/api/state', headers=ADMIN).json()
    node = next(value for value in state['nodes'] if value['id'] == NODE)
    assert node['network_status'] == 'stale' and node['network_rx_bps'] is None
    assert node['network_rx'] == 123456789


def test_new_probe_metrics_history_and_summary_preserve_real_measurements(hub):
    module, client = hub
    module.proxy_status[NODE + '.ssh'] = 'online'
    module.proxy_status_updated = module.time.monotonic()
    samples = [{'timestamp': NOW - 30 + i, 'success': True, 'latency_ms': 10 if i < 18 else 1000} for i in range(20)]
    data = {'cpu_percent': 22, 'memory_percent': 33, 'disk_percent': 44,
            'memory_used_bytes': 200, 'memory_total_bytes': 800, 'disk_used_bytes': 1000, 'disk_total_bytes': 2000,
            'gpu_status': 'no_gpu', 'gpus': [], 'latency_samples': samples,
            'latency_ms': 1000, 'latency_sample_at': NOW - 11, 'latency_success': True}
    path = '/api/nodes/' + NODE + '/heartbeat'
    assert client.post(path, headers=NODE_AUTH, json=data).status_code == 200
    assert client.post(path, headers=NODE_AUTH, json=data).status_code == 200
    nodes = client.get('/api/state', headers=ADMIN).json()['nodes']
    node = next(item for item in nodes if item['id'] == NODE)
    assert node['online'] is True
    assert node['freshness'] == {'status': 'fresh', 'age_seconds': 0, 'sampled_at': NOW}
    assert node['gpu_percent'] is None and node['gpus'] == []
    assert node['memory_used_bytes'] == 200
    assert 'latency_samples' not in node and 'token' not in node and 'token_hash' not in node
    history = client.get('/api/nodes/' + NODE + '/history?metric=cpu&range=1h', headers=ADMIN).json()
    assert len(history['points']) == 60
    assert history['points'][-1]['value'] == 22
    assert all(point['value'] is None for point in history['points'][:-1])
    assert history['stats'] == {'min': 22, 'max': 22, 'avg': 22}
    summary = client.get('/api/latency/summary?range=24h&bucket=15m', headers=ADMIN).json()
    assert summary['range'] == '24h' and summary['bucket_seconds'] == 900
    assert summary['thresholds'] == {'healthy_ms': 100, 'critical_ms': 250}
    assert REVOKED not in summary['nodes'] and set(summary['nodes']) == {NODE, OTHER}
    buckets = summary['nodes'][NODE]['buckets']
    assert len(buckets) == 96 and sum(bucket['sample_count'] for bucket in buckets) == 20
    assert summary['nodes'][NODE]['p95_ms'] == 1000
    assert summary['nodes'][NODE]['avg_ms'] == 109
    assert all(bucket['status'] == 'unknown' for bucket in summary['nodes'][OTHER]['buckets'])


@pytest.mark.parametrize('metadata,status,age', [
    ({'metrics_status': 'ok', 'metrics_sampled_at': NOW - 2}, 'fresh', 2),
    ({'metrics_status': 'ok', 'metrics_sampled_at': NOW - 35}, 'stale', 35),
    ({'metrics_status': 'collecting', 'metrics_sampled_at': None}, 'unknown', None),
    ({'metrics_status': 'stale', 'metrics_sampled_at': NOW - 40}, 'stale', 40),
    ({'metrics_status': 'error', 'metrics_sampled_at': NOW - 1, 'metrics_error': 'resource collection failed'}, 'stale', 1),
    ({'metrics_status': 'ok', 'metrics_sampled_at': NOW + 61}, 'stale', 0),
    ({}, 'fresh', 0),
])
def test_state_distinguishes_resource_sampling_from_heartbeat_freshness(hub, metadata, status, age):
    _, client = hub
    assert client.post('/api/nodes/' + NODE + '/heartbeat', headers=NODE_AUTH, json=metadata).status_code == 200
    node = next(item for item in client.get('/api/state', headers=ADMIN).json()['nodes'] if item['id'] == NODE)
    assert node['heartbeat_freshness'] == {'status': 'fresh', 'age_seconds': 0, 'sampled_at': NOW}
    assert node['freshness']['status'] == status
    assert node['metrics_age_seconds'] == age
    assert node['metrics_stale'] is (status != 'fresh')
    if 'metrics_status' in metadata:
        assert node['metrics_status'] == metadata['metrics_status']
        assert node['freshness']['sampled_at'] == metadata['metrics_sampled_at']


def test_reused_resource_sample_is_not_recorded_again_after_store_restart_and_minute_boundary(hub, monkeypatch):
    module, client = hub
    first_at = NOW // 60 * 60 + 58
    monkeypatch.setattr(module.time, 'time', lambda: first_at)
    path = '/api/nodes/' + NODE + '/heartbeat'
    data = {'cpu_percent': 10, 'memory_percent': 20, 'disk_percent': 30,
            'metrics_status': 'ok', 'metrics_sampled_at': first_at - 1,
            'gpus': [{'index': 0, 'util_percent': 20}],
            'latency_sample_at': first_at - 1, 'latency_ms': 10}
    assert client.post(path, headers=NODE_AUTH, json=data).status_code == 200
    # A new store has no process-local dedup state. The persisted node sample
    # timestamp still prevents repeated cached CPU/memory/disk observations.
    module.probes = type(module.probes)(module.db)
    later = first_at + 4
    monkeypatch.setattr(module.time, 'time', lambda: later)
    data.update(gpus=[{'index': 0, 'util_percent': 80}], latency_sample_at=later - 1, latency_ms=30)
    assert client.post(path, headers=NODE_AUTH, json=data).status_code == 200
    for metric in ('cpu', 'memory', 'disk'):
        history = client.get('/api/nodes/' + NODE + '/history?metric=' + metric, headers=ADMIN).json()
        assert history['points'][-2]['value'] is not None
        assert history['points'][-1]['value'] is None
    gpu_history = client.get('/api/nodes/' + NODE + '/history?metric=gpu', headers=ADMIN).json()
    assert gpu_history['points'][-2]['value'] == 20
    assert gpu_history['points'][-1]['value'] == 80
    summary = client.get('/api/latency/summary', headers=ADMIN).json()['nodes'][NODE]
    assert sum(bucket['sample_count'] for bucket in summary['buckets']) == 2
    assert summary['avg_ms'] == 20
    data.update(metrics_sampled_at=later, cpu_percent=90)
    assert client.post(path, headers=NODE_AUTH, json=data).status_code == 200
    history = client.get('/api/nodes/' + NODE + '/history?metric=cpu', headers=ADMIN).json()
    assert history['points'][-1]['value'] == 90


@pytest.mark.parametrize('metadata', [
    {'metrics_status': 'collecting', 'metrics_sampled_at': None},
    {'metrics_status': 'error', 'metrics_sampled_at': NOW - 1},
    {'metrics_status': 'ok', 'metrics_sampled_at': NOW - 35},
    {'metrics_status': 'ok', 'metrics_sampled_at': NOW + 61},
    {'metrics_status': 'ok', 'metrics_sampled_at': None},
])
def test_unsuccessful_or_invalid_resource_observation_is_not_added_to_history(hub, metadata):
    _, client = hub
    data = dict(metadata, cpu_percent=90, memory_percent=80, disk_percent=70,
                gpus=[{'index': 0, 'util_percent': 50}], latency_sample_at=NOW - 1, latency_ms=10)
    assert client.post('/api/nodes/' + NODE + '/heartbeat', headers=NODE_AUTH, json=data).status_code == 200
    for metric in ('cpu', 'memory', 'disk'):
        history = client.get('/api/nodes/' + NODE + '/history?metric=' + metric, headers=ADMIN).json()
        assert history['stats']['avg'] is None
    gpu_history = client.get('/api/nodes/' + NODE + '/history?metric=gpu', headers=ADMIN).json()
    assert gpu_history['stats']['avg'] == 50
    assert client.get('/api/latency/summary', headers=ADMIN).json()['nodes'][NODE]['avg_ms'] == 10


def test_state_fetches_manual_locations_without_per_node_metadata_reads(hub):
    module, client = hub
    module.probes.set_location(NODE, {'country_code': 'JP', 'city': 'Tokyo'})
    queries = []
    module.db.set_trace_callback(queries.append)
    try:
        response = client.get('/api/state', headers=ADMIN)
    finally:
        module.db.set_trace_callback(None)
    assert response.status_code == 200
    nodes = {item['id']: item for item in response.json()['nodes']}
    assert nodes[NODE]['country_code'] == 'JP'
    assert nodes[OTHER]['location_source'] == 'unknown'
    assert not any(query.startswith('SELECT manual_location FROM probe_metadata') for query in queries)
    assert sum('LEFT JOIN probe_metadata' in query for query in queries) == 1


def test_manual_location_cannot_be_overwritten_by_node_heartbeat(hub):
    _, client = hub
    path = '/api/nodes/' + NODE
    assert client.put(path + '/location', headers=ADMIN,
                      json={'country_code': 'cn', 'country_name': '中国', 'region': '', 'city': '上海'}).status_code == 200
    assert client.post(path + '/heartbeat', headers=NODE_AUTH,
                       json={'country_code': 'US', 'country_name': '美国', 'city': '纽约'}).status_code == 200
    node = next(item for item in client.get('/api/state', headers=ADMIN).json()['nodes'] if item['id'] == NODE)
    assert node['country_code'] == 'CN' and node['city'] == '上海'
    assert node['location_source'] == 'manual'
    assert client.put(path + '/location', headers=ADMIN, json={'country_code': 'invalid'}).status_code == 400


def test_stale_state_and_hub_monitor_gap_are_not_fabricated(hub):
    module, client = hub
    module.db.execute('UPDATE nodes SET last_seen=?,metrics=? WHERE id=?',
                      (NOW - 3600, json.dumps({'cpu_percent': 91}), NODE))
    module.db.commit()
    nodes = [{'id': NODE, 'last_seen': NOW - 3600, 'frp_status': None}]
    module.probes.mark_offline(nodes, NOW - 3600)
    module.probes.mark_offline(nodes, NOW)
    state = next(item for item in client.get('/api/state', headers=ADMIN).json()['nodes'] if item['id'] == NODE)
    assert state['cpu_percent'] == 91 and state['freshness']['status'] == 'stale'
    assert state['online'] is False
    summary = client.get('/api/latency/summary', headers=ADMIN).json()['nodes'][NODE]
    assert all(bucket['status'] == 'unknown' for bucket in summary['buckets'])
    assert summary['avg_ms'] is None


def test_gpu_device_history_and_invalid_api_parameters(hub):
    _, client = hub
    assert client.post('/api/nodes/' + NODE + '/heartbeat', headers=NODE_AUTH, json={
        'gpus': [{'index': 0, 'vendor': 'nvidia', 'name': 'A', 'util_percent': 30},
                 {'index': 1, 'vendor': 'nvidia', 'name': 'B', 'util_percent': 70}], 'gpu_status': 'ok'}).status_code == 200
    path = '/api/nodes/' + NODE + '/history'
    response = client.get(path + '?metric=gpu&device=1&range=6h', headers=ADMIN)
    assert response.status_code == 200
    assert response.json()['points'][-1]['value'] == 70
    assert len(response.json()['devices']) == 2
    for query in ('metric=unknown', 'metric=cpu&range=7d', 'metric=gpu&device=-1', 'metric=cpu&device=1'):
        assert client.get(path + '?' + query, headers=ADMIN).status_code == 400
    assert client.get('/api/latency/summary?bucket=1m', headers=ADMIN).status_code == 400
    for node_id in ('missing', REVOKED):
        assert client.get('/api/nodes/' + node_id + '/history', headers=ADMIN).status_code == 404
        assert client.put('/api/nodes/' + node_id + '/location', headers=ADMIN, json={}).status_code == 404


def test_summary_cache_reuses_calculation_across_requests_and_heartbeats(hub, monkeypatch):
    module, client = hub
    module.probes.record(NODE, {'latency_ms': 150, 'latency_sample_at': NOW - 1}, NOW)
    calculate = mock.Mock(wraps=module.probes.latency_summary)
    monkeypatch.setattr(module.probes, 'latency_summary', calculate)
    first = client.get('/api/latency/summary', headers=ADMIN)
    assert first.status_code == 200
    assert client.post('/api/nodes/' + NODE + '/heartbeat', headers=NODE_AUTH,
                       json={'latency_ms': 50, 'latency_sample_at': NOW}).status_code == 200
    repeated = client.get('/api/latency/summary', headers=ADMIN)
    assert repeated.status_code == 200
    assert repeated.json() == first.json()
    assert repeated.json()['nodes'][NODE]['avg_ms'] == 150
    assert calculate.call_count == 1


def test_summary_cache_isolates_both_threshold_parameters(hub, monkeypatch):
    module, client = hub
    module.probes.record(NODE, {'latency_ms': 150, 'latency_sample_at': NOW - 1}, NOW)
    calculate = mock.Mock(wraps=module.probes.latency_summary)
    monkeypatch.setattr(module.probes, 'latency_summary', calculate)
    for query, expected in [('', 'degraded'), ('?healthy_ms=200', 'healthy'),
                            ('?critical_ms=120', 'critical'), ('?healthy_ms=200', 'healthy')]:
        response = client.get('/api/latency/summary' + query, headers=ADMIN)
        assert response.status_code == 200
        assert response.json()['nodes'][NODE]['buckets'][-1]['status'] == expected
    assert calculate.call_count == 3


def test_summary_cache_immediately_removes_node_after_revoke(hub, monkeypatch):
    module, client = hub
    calculate = mock.Mock(wraps=module.probes.latency_summary)
    monkeypatch.setattr(module.probes, 'latency_summary', calculate)
    first = client.get('/api/latency/summary', headers=ADMIN)
    assert first.status_code == 200
    assert set(first.json()['nodes']) == {NODE, OTHER}
    assert client.post('/api/nodes/' + OTHER + '/revoke', headers=ADMIN).status_code == 200
    current = client.get('/api/latency/summary', headers=ADMIN)
    assert current.status_code == 200
    assert set(current.json()['nodes']) == {NODE}
    assert calculate.call_count == 2


def test_warm_summary_cache_does_not_bypass_auth_or_parameter_validation(hub, monkeypatch):
    module, client = hub
    calculate = mock.Mock(wraps=module.probes.latency_summary)
    monkeypatch.setattr(module.probes, 'latency_summary', calculate)
    assert client.get('/api/latency/summary', headers=ADMIN).status_code == 200
    for headers in ({}, {'Authorization': 'Bearer wrong'}, NODE_AUTH):
        assert client.get('/api/latency/summary', headers=headers).status_code == 401
    for query in ('healthy_ms=-1', 'healthy_ms=251&critical_ms=250', 'healthy_ms=nan', 'critical_ms=inf'):
        assert client.get('/api/latency/summary?' + query, headers=ADMIN).status_code == 400
    assert calculate.call_count == 1
