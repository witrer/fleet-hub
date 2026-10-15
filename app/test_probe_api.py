"""HTTP contracts for the integrated probe API, with unrelated OAuth installation isolated."""
import importlib
import importlib.util
import json
import os
from pathlib import Path

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
    monkeypatch.setattr(oauth, 'install_oauth', lambda *args: None)
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


def test_new_probe_metrics_history_and_summary_preserve_real_measurements(hub):
    module, client = hub
    module.proxy_status[NODE + '.ssh'] = 'online'
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
