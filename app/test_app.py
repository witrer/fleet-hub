import importlib.util
import json
import shlex
from pathlib import Path

import asyncssh
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def hub(tmp_path, monkeypatch):
    (tmp_path / 'keys').mkdir()
    (tmp_path / '.ssh').mkdir()
    (tmp_path / 'ca.pem').write_text('synthetic test CA')
    (tmp_path / 'config.json').write_text(json.dumps({
        'base_url': 'https://hub.test', 'host': 'hub.test',
        'admin_token': 'synthetic-admin-token', 'frp_dashboard_password': 'synthetic',
        'assets': {'linux_amd64': {'name': 'frp-test.tar.gz', 'sha256': '0' * 64}},
    }))
    monkeypatch.setenv('FLEET_ROOT', str(tmp_path))
    spec = importlib.util.spec_from_file_location('fleet_test_app', Path(__file__).with_name('app.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.refresh_inventory()
    client = TestClient(module.app)
    yield module, client, tmp_path
    client.close()
    module.db.close()


ADMIN = {'Authorization': 'Bearer synthetic-admin-token'}


def grant(client):
    response = client.post('/api/enrollments', headers=ADMIN, json={'name': 'training-test'})
    assert response.status_code == 200
    token = shlex.split(response.json()['command'])[-1]
    return {'Authorization': 'Bearer ' + token}


def body():
    return {'ssh_user': 'root', 'ssh_port': 22, 'platform': 'linux_amd64',
            'ssh_host_key': asyncssh.generate_private_key('ssh-ed25519').export_public_key().decode()}


def test_admin_auth_and_state_shape(hub):
    _, client, _ = hub
    assert client.get('/api/state').status_code == 401
    assert client.post('/api/enrollments', json={}).status_code == 401
    assert client.get('/api/state', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    response = client.get('/api/state', headers=ADMIN)
    assert response.status_code == 200
    assert response.json() == {'nodes': [], 'transfers': [], 'mcp_url': 'https://hub.test/mcp', 'version': '1.0.0', 'update': {}}


@pytest.mark.parametrize('content', ['{incomplete', '[]', 'null'])
def test_invalid_update_status_does_not_break_state(hub, content):
    _, client, root = hub
    (root / 'update-status.json').write_text(content)
    response = client.get('/api/state', headers=ADMIN)
    assert response.status_code == 200
    assert response.json()['update'] == {}


def test_update_status_is_admin_only_and_exposes_display_fields(hub):
    _, client, root = hub
    expected = {
        'current_commit': 'a' * 40, 'target_commit': 'b' * 40,
        'checked_at': '2026-10-15T12:00:00Z', 'deployed_at': '2026-10-15T11:00:00Z',
        'status': 'rolled_back', 'message': '健康检查失败，已恢复上一版本',
    }
    (root / 'update-status.json').write_text(json.dumps({**expected, 'internal_detail': 'not-for-api'}))
    assert client.get('/api/state').status_code == 401
    assert client.get('/api/state', headers=ADMIN).json()['update'] == expected


def test_enroll_once_strict_keys_and_node_auth(hub):
    module, client, root = hub
    headers = grant(client)
    payload = body()
    response = client.post('/api/enroll', headers=headers, json=payload)
    assert response.status_code == 200
    node = response.json()
    assert {'id', 'token', 'name', 'public_key', 'frpc_config', 'ca_pem', 'download_url', 'sha256'} <= node.keys()
    assert client.post('/api/enroll', headers=headers, json=payload).status_code == 401
    assert module.db.execute('SELECT count(*) FROM nodes').fetchone()[0] == 1
    inv = json.loads((root / 'inventory.json').read_text())[node['id']]
    assert inv['known_hosts'] == 'strict'
    known = asyncssh.read_known_hosts(str(root / '.ssh' / 'known_hosts')).match('127.0.0.1', '', inv['port'])[0]
    assert asyncssh.import_public_key(payload['ssh_host_key']) in known
    assert asyncssh.read_private_key(inv['identity_file']).export_public_key().decode().strip() in node['public_key']
    heartbeat = '/api/nodes/' + node['id'] + '/heartbeat'
    assert client.post(heartbeat, json={}).status_code == 401
    assert client.post(heartbeat, headers={'Authorization': 'Bearer ' + node['token']}, json={'cpu_percent': 7}).status_code == 200
    module.proxy_status[node['id'] + '.ssh'] = 'online'
    public = client.get('/api/state', headers=ADMIN).json()['nodes'][0]
    assert public['online'] is True and public['cpu_percent'] == 7
    assert 'token' not in public and 'secret' not in public


def test_rejected_enrollment_does_not_consume_grant(hub):
    module, client, root = hub
    headers = grant(client)
    payload = body()
    for bad in ({'platform': 'darwin'}, {'ssh_port': 'invalid'}, {'ssh_port': 70000}, {'ssh_host_key': 'bad'}):
        assert client.post('/api/enroll', headers=headers, json={**payload, **bad}).status_code == 400
        assert module.db.execute('SELECT count(*) FROM enrollments').fetchone()[0] == 1
        assert module.db.execute('SELECT count(*) FROM nodes').fetchone()[0] == 0
        assert list((root / 'keys').iterdir()) == []
    assert client.post('/api/enroll', headers=headers, json=payload).status_code == 200


def test_transfer_expiry_uses_target_local_time_for_openssh_89(hub, monkeypatch):
    from types import SimpleNamespace
    module, client, _ = hub
    payload = body()
    node = client.post('/api/enroll', headers=grant(client), json=payload).json()
    commands = []

    class Connection:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def run(self, command, **kwargs):
            commands.append(command)
            return SimpleNamespace(stdout='20261016124500\n')

    monkeypatch.setattr(module.asyncssh, 'connect', lambda *args, **kwargs: Connection())
    response = client.post('/api/transfers', headers=ADMIN, json={
        'node_id': node['id'], 'public_key': payload['ssh_host_key'], 'bytes_total': 31,
    })
    assert response.status_code == 200
    assert commands[0].startswith('date -d @')
    assert 'expiry-time="20261016124500"' in commands[1]
    assert '20261016124500Z' not in commands[1]


def test_agent_release_matches_the_downloaded_script(hub):
    import hashlib
    _, client, _ = hub
    release=client.get('/api/agent-release')
    assert release.status_code==200
    payload=release.json()
    downloaded=client.get('/node.py')
    assert hashlib.sha256(downloaded.content).hexdigest()==payload['sha256']
    assert payload['version']==payload['sha256'][:12]
    assert payload['url']=='https://hub.test/node.py'


@pytest.mark.parametrize('path', ['/install.sh', '/send.sh', '/send.ps1'])
def test_one_command_launchers_use_the_configured_hub(hub, path):
    _, client, _ = hub
    response = client.get(path)
    assert response.status_code == 200
    assert 'https://hub.test' in response.text
    assert '__HUB_URL__' not in response.text
