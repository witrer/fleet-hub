import importlib.util
import json
import shlex
from pathlib import Path

import asyncssh
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def hub(tmp_path, monkeypatch, request):
    (tmp_path / 'keys').mkdir()
    (tmp_path / '.ssh').mkdir()
    (tmp_path / 'ca.pem').write_text('synthetic test CA')
    (tmp_path / 'config.json').write_text(json.dumps({
        'base_url': 'https://hub.test', 'host': 'hub.test',
        'admin_token': 'synthetic-admin-token', 'frp_dashboard_password': 'synthetic',
        'assets': {'linux_amd64': {'name': 'frp-test.tar.gz', 'sha256': '0' * 64}},
        **getattr(request, 'param', {}),
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


@pytest.fixture
def transfer_record(hub):
    module, client, _ = hub
    node = client.post('/api/enroll', headers=grant(client), json=body()).json()
    transfer_id, token = 'transfer-progress-test', 'synthetic-transfer-token'
    module.db.execute('''INSERT INTO transfers
        (id,node_id,token_hash,expires,status,phase,bytes_total,updated_at)
        VALUES(?,?,?,?,?,?,?,?)''',
        (transfer_id, node['id'], module.digest(token), module.time.time()+86400,
         'connecting', 'preparing_tool', 100, module.time.time()))
    module.db.commit()
    return module, client, transfer_id, token, node['id']


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
    state = response.json()
    diagnostics = state.pop('hub')
    storage = state.pop('storage')['onedrive']
    assert storage['enabled'] is storage['configured'] is storage['connected'] is False
    assert storage['auth_state'] == 'unconfigured' and storage['quota'] is None
    assert storage['cleanup']['mode'] == 'permanent' and storage['cleanup']['pending_count'] == 0
    assert state == {'nodes': [], 'transfers': [], 'mcp_url': 'https://hub.test/mcp', 'version': '1.1.0', 'update': {}}
    assert diagnostics['monitor']['status'] == 'starting'
    assert diagnostics['frp']['status'] == 'unknown'
    assert diagnostics['uptime_seconds'] >= 0


@pytest.mark.parametrize('hub', [{'oauth_base_url': 'https://auth.hub.test'}], indirect=True)
def test_oauth_domain_preserves_node_download_origin(hub):
    _, client, _ = hub
    assert client.get('/api/state', headers=ADMIN).json()['mcp_url'] == 'https://auth.hub.test/mcp'
    metadata = client.get('/.well-known/oauth-authorization-server').json()
    assert metadata['issuer'] == 'https://auth.hub.test/'
    assert metadata['code_challenge_methods_supported'] == ['S256']
    assert client.get('/api/agent-release').json()['url'] == 'https://hub.test/node.py'
    assert 'https://hub.test' in client.get('/install.sh').text
    command = client.post('/api/enrollments', headers=ADMIN, json={}).json()['command']
    assert command.startswith('curl -fsSL https://hub.test/install.sh | ')


@pytest.mark.parametrize('content', ['{incomplete', '[]', 'null'])
def test_invalid_update_status_does_not_break_state(hub, content):
    _, client, root = hub
    (root / 'update-status.json').write_text(content)
    response = client.get('/api/state', headers=ADMIN)
    assert response.status_code == 200
    assert response.json()['update'] == {}


def test_transfer_script_validator_reuses_bytes_and_invalidates_on_change(hub, monkeypatch, tmp_path):
    import hashlib
    module, client, _ = hub
    code = tmp_path / 'code'
    (code / 'static').mkdir(parents=True)
    path = code / 'static' / 'transfer.py'
    path.write_bytes(b'# first script\n')
    monkeypatch.setattr(module, 'CODE', code)
    first = client.get('/transfer.py')
    etag = '"' + hashlib.sha256(first.content).hexdigest() + '"'
    assert first.status_code == 200 and first.headers['etag'] == etag
    assert first.headers['cache-control'] == 'no-cache'
    read = Path.read_bytes
    def reject_rehash(self):
        if self == path:
            raise AssertionError('Unchanged source must not be hashed for every client')
        return read(self)
    with monkeypatch.context() as context:
        context.setattr(Path, 'read_bytes', reject_rehash)
        cached = client.get('/transfer.py', headers={'If-None-Match': '"other", W/' + etag})
        assert cached.status_code == 304 and cached.content == b''
    path.write_bytes(b'# newer script\n')
    changed = client.get('/transfer.py', headers={'If-None-Match': etag})
    assert changed.status_code == 200 and changed.content == b'# newer script\n'
    assert changed.headers['etag'] != etag


def test_release_manifest_binds_agent_and_supervisor_bytes(hub):
    import hashlib
    _, client, _ = hub
    release = client.get('/api/agent-release').json()
    assert release['sha256'] == hashlib.sha256(client.get('/node.py').content).hexdigest()
    supervisor = release['supervisor']
    assert supervisor['url'] == 'https://hub.test/node-supervisor.py'
    assert supervisor['sha256'] == hashlib.sha256(client.get('/node-supervisor.py').content).hexdigest()


def test_heartbeat_defers_agent_restart_for_authorized_active_transfer(hub):
    module, client, _ = hub
    node = client.post('/api/enroll', headers=grant(client), json=body()).json()
    auth = {'Authorization': 'Bearer ' + node['token']}
    url = '/api/nodes/' + node['id'] + '/heartbeat'
    assert client.post(url, headers=auth, json={}).json()['update_allowed'] is True
    module.db.execute('INSERT INTO transfers(id,node_id,status,expires,progress_stale) VALUES(?,?,?,?,?)',
                      ('active-update-test', node['id'], 'transferring', module.time.time()+86400, 1))
    module.db.commit()
    assert client.post(url, headers=auth, json={}).json()['update_allowed'] is False
    module.db.execute("UPDATE transfers SET status='completed' WHERE id='active-update-test'")
    module.db.commit()
    assert client.post(url, headers=auth, json={}).json()['update_allowed'] is True
    module.db.execute("UPDATE transfers SET status='connecting',expires=? WHERE id='active-update-test'",
                      (module.time.time()-1,))
    module.db.commit()
    assert client.post(url, headers=auth, json={}).json()['update_allowed'] is True


@pytest.mark.parametrize('fail', [False, True])
def test_heartbeat_defers_update_while_transfer_authorization_is_in_flight(hub, monkeypatch, fail):
    import asyncio
    import httpx
    from types import SimpleNamespace
    module, client, _ = hub
    payload = body()
    node = client.post('/api/enroll', headers=grant(client), json=payload).json()
    url = '/api/nodes/' + node['id'] + '/heartbeat'
    auth = {'Authorization': 'Bearer ' + node['token']}

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class Connection:
            async def __aenter__(self):
                entered.set()
                await release.wait()
                if fail:
                    raise ConnectionError('synthetic connection failure')
                return self
            async def __aexit__(self, *args): pass
            async def run(self, *args, **kwargs):
                return SimpleNamespace(stdout='20261016124500\n')
        monkeypatch.setattr(module.asyncssh, 'connect', lambda *args, **kwargs: Connection())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url='https://hub.test') as local:
            preparing = asyncio.create_task(local.post('/api/transfers', headers=ADMIN, json={
                'node_id': node['id'], 'public_key': payload['ssh_host_key'],
            }))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                assert module.db.execute('SELECT count(*) FROM transfers').fetchone()[0] == 0
                assert (await local.post(url, headers=auth, json={})).json()['update_allowed'] is False
            finally:
                release.set()
                response = await asyncio.wait_for(preparing, 2)
            assert response.status_code == (502 if fail else 200)
            assert not module.transfer_preparations
            # A successful preparation hands protection over to the stored task.
            assert (await local.post(url, headers=auth, json={})).json()['update_allowed'] is fail
    asyncio.run(scenario())


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
    module.proxy_status_updated = module.time.monotonic()
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


@pytest.mark.parametrize('stage', ['ssh_connect', 'target_clock', 'authorize_key'])
def test_transfer_preparation_has_total_deadline_and_reports_stage(hub, monkeypatch, stage):
    import asyncio
    from types import SimpleNamespace
    module, client, _ = hub
    payload = body()
    node = client.post('/api/enroll', headers=grant(client), json=payload).json()

    async def wait():
        await asyncio.Event().wait()

    class Connection:
        async def __aenter__(self):
            if stage == 'ssh_connect': await wait()
            return self
        async def __aexit__(self, *args): pass
        async def run(self, command, **kwargs):
            if (command.startswith('date ') and stage == 'target_clock' or
                    not command.startswith('date ') and stage == 'authorize_key'):
                await wait()
            return SimpleNamespace(stdout='20261016124500\n')

    monkeypatch.setattr(module, 'TRANSFER_PREPARE_TIMEOUT', .01)
    monkeypatch.setattr(module.asyncssh, 'connect', lambda *args, **kwargs: Connection())
    response = client.post('/api/transfers', headers=ADMIN, json={
        'node_id': node['id'], 'public_key': payload['ssh_host_key'], 'bytes_total': 31,
    })
    assert response.status_code == 504
    assert stage in response.json()['detail']
    assert module.db.execute('SELECT count(*) FROM transfers').fetchone()[0] == 0


def test_transfer_progress_keeps_phase_timing_and_accepts_legacy_clients(hub, monkeypatch):
    from types import SimpleNamespace
    module, client, _ = hub
    payload = body()
    node = client.post('/api/enroll', headers=grant(client), json=payload).json()

    class Connection:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def run(self, *args, **kwargs):
            return SimpleNamespace(stdout='20261016124500\n')

    monkeypatch.setattr(module.asyncssh, 'connect', lambda *args, **kwargs: Connection())
    job = client.post('/api/transfers', headers=ADMIN, json={
        'node_id': node['id'], 'public_key': payload['ssh_host_key'], 'bytes_total': 100,
    }).json()
    url = '/api/transfers/' + job['id'] + '/progress'
    auth = {'Authorization': 'Bearer ' + job['token']}
    progress = {'status': 'transferring', 'phase': 'transferring', 'bytes_done': 50,
                'speed_bps': 10, 'elapsed_seconds': 185, 'transfer_seconds': 5}
    assert client.post(url, headers=auth, json=progress).status_code == 200
    stored = client.get('/api/state', headers=ADMIN).json()['transfers'][0]
    assert all(stored[key] == value for key, value in progress.items())
    for bad in ({'phase': 'unknown'}, {'phase': []}, {'elapsed_seconds': -1},
                {'transfer_seconds': True}, {'elapsed_seconds': '185'}):
        assert client.post(url, headers=auth, json={**progress, **bad}).status_code == 400
    # Old uploaders omit phase/timers; retained history must not be erased.
    assert client.post(url, headers=auth, json={'status': 'completed', 'bytes_done': 100}).status_code == 200
    stored = client.get('/api/state', headers=ADMIN).json()['transfers'][0]
    assert stored['phase'] == 'completed'
    assert stored['elapsed_seconds'] == 185 and stored['transfer_seconds'] == 5


def test_transfer_schema_upgrade_preserves_existing_history(hub):
    import sqlite3
    module, _, _ = hub
    with sqlite3.connect(':memory:') as legacy:
        legacy.execute('CREATE TABLE transfers(id TEXT, status TEXT)')
        legacy.execute("INSERT INTO transfers VALUES('old-job', 'completed')")
        module.ensure_transfer_fields(legacy)
        module.ensure_transfer_fields(legacy)
        assert legacy.execute('SELECT * FROM transfers').fetchone() == ('old-job', 'completed', None, None, None, 0)


def test_fresh_heartbeat_with_unknown_tunnel_is_not_confirmed_offline(hub):
    module, client, _ = hub
    node = client.post('/api/enroll', headers=grant(client), json=body()).json()
    client.post('/api/nodes/' + node['id'] + '/heartbeat',
                headers={'Authorization': 'Bearer ' + node['token']}, json={})
    public = client.get('/api/state', headers=ADMIN).json()['nodes'][0]
    assert public['connection_status'] == 'unknown' and public['online'] is False
    module.proxy_status[node['id'] + '.ssh'] = 'offline'
    module.proxy_status_updated = module.time.monotonic()
    assert client.get('/api/state', headers=ADMIN).json()['nodes'][0]['connection_status'] == 'offline'
    module.proxy_status[node['id'] + '.ssh'] = 'online'
    assert client.get('/api/state', headers=ADMIN).json()['nodes'][0]['connection_status'] == 'online'
    module.proxy_status.clear()
    module.db.execute('UPDATE nodes SET last_seen=0 WHERE id=?', (node['id'],))
    assert client.get('/api/state', headers=ADMIN).json()['nodes'][0]['connection_status'] == 'offline'


def test_monitor_failure_keeps_node_unknown_and_marks_progress_stale(hub, monkeypatch):
    import asyncio
    module, client, _ = hub
    node = client.post('/api/enroll', headers=grant(client), json=body()).json()
    client.post('/api/nodes/' + node['id'] + '/heartbeat',
                headers={'Authorization': 'Bearer ' + node['token']}, json={})
    module.proxy_status[node['id'] + '.ssh'] = 'online'
    module.db.execute('INSERT INTO transfers(id,node_id,status,phase,updated_at) VALUES(?,?,?,?,?)',
                      ('stale-job', node['id'], 'transferring', 'transferring', module.time.time()-100))
    module.db.commit()

    class DashboardUnavailable:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *args, **kwargs): raise TimeoutError('synthetic dashboard timeout')

    class EndIteration(Exception): pass
    async def stop(_): raise EndIteration

    with monkeypatch.context() as context:
        context.setattr(module.httpx, 'AsyncClient', lambda **kwargs: DashboardUnavailable())
        context.setattr(module.asyncio, 'sleep', stop)
        with pytest.raises(EndIteration):
            asyncio.run(module.monitor())
    state = client.get('/api/state', headers=ADMIN).json()
    assert state['nodes'][0]['connection_status'] == 'unknown'
    assert state['transfers'][0]['status'] == state['transfers'][0]['phase'] == 'transferring'
    assert state['transfers'][0]['progress_stale'] is True
    assert state['transfers'][0]['updated_at'] < module.time.time()-90
    assert state['transfers'][0]['error'] == ''
    assert state['hub']['frp']['error_code'] == 'TimeoutError'
    assert state['hub']['monitor']['status'] == 'degraded'
    assert state['hub']['monitor']['cycle_ms'] >= 0


def test_expired_tunnel_snapshot_does_not_confirm_online(hub, monkeypatch):
    module, client, _ = hub
    node = client.post('/api/enroll', headers=grant(client), json=body()).json()
    client.post('/api/nodes/' + node['id'] + '/heartbeat',
                headers={'Authorization': 'Bearer ' + node['token']}, json={})
    module.proxy_status[node['id'] + '.ssh'] = 'online'
    module.proxy_status_updated = module.time.monotonic() - module.MONITOR_STALE_AFTER
    module.monitor_completed = module.proxy_status_updated
    state = client.get('/api/state', headers=ADMIN).json()
    assert state['nodes'][0]['connection_status'] == 'unknown'
    assert state['nodes'][0]['frp_status'] is None
    assert state['hub']['frp']['status'] == 'stale'
    assert state['hub']['monitor']['status'] == 'stale'


def test_monitor_survives_database_error_and_recovers(hub, monkeypatch):
    import asyncio
    import sqlite3
    from types import SimpleNamespace
    module, client, _ = hub
    calls, reports = [], []
    original = module.probes.mark_offline

    def fail_once(nodes):
        calls.append(nodes)
        if len(calls) == 1:
            raise sqlite3.OperationalError('sensitive synthetic detail')
        return original(nodes)

    class Dashboard:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, *args, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {'proxies': [{'name': 'test.ssh', 'status': 'online'}]})

    class EndIterations(Exception): pass
    async def pause(delay):
        assert 0 < delay <= module.MONITOR_INTERVAL
        reports.append(module.hub_diagnostics())
        if len(reports) == 2:
            raise EndIterations

    with monkeypatch.context() as context:
        context.setattr(module.probes, 'mark_offline', fail_once)
        context.setattr(module.httpx, 'AsyncClient', lambda **kwargs: Dashboard())
        context.setattr(module.asyncio, 'sleep', pause)
        with pytest.raises(EndIterations):
            asyncio.run(module.monitor())
    assert len(calls) == 2
    assert reports[0]['monitor']['status'] == 'degraded'
    assert reports[0]['monitor']['error_code'] == 'OperationalError'
    assert 'sensitive' not in json.dumps(reports)
    assert reports[1]['monitor']['status'] == 'ok'
    assert reports[1]['monitor']['error_code'] is None
    assert reports[1]['monitor']['last_success_at'] is not None
    assert reports[1]['frp']['status'] == 'ok'


def test_startup_and_shutdown_expose_diagnostics_and_close_monitor(hub, monkeypatch):
    from types import SimpleNamespace
    module, client, _ = hub
    closed = []
    class Dashboard:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): closed.append(True)
        async def get(self, *args, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'proxies': []})
    monkeypatch.setattr(module.httpx, 'AsyncClient', lambda **kwargs: Dashboard())
    with client:
        assert client.get('/health').json() == {'ok': True}
        response = client.get('/api/state', headers=ADMIN)
        assert response.status_code == 200
        diagnostics = response.json()['hub']
        assert diagnostics['startup_ms'] >= 0
        assert diagnostics['monitor']['status'] == 'ok'
        assert diagnostics['frp']['status'] == 'ok'
    assert closed == [True]


def test_hub_network_uses_cached_observation_and_hides_expired_rates(hub, monkeypatch):
    module, client, _ = hub
    payload = {'network_status': 'ok', 'network_sampled_at': module.time.time(),
                          'network_rx_bps': 1234, 'network_tx_bps': 5678,
                          'network_rx': 9999, 'network_interfaces': ['eth0']}
    module.hub_network_sampler.cached = (module.time.monotonic(), payload)
    def no_collection():
        raise AssertionError('State polling must not collect network counters')
    monkeypatch.setattr(module.hub_network_sampler, 'sample', no_collection)
    observation = client.get('/api/state', headers=ADMIN).json()['hub']['network']
    assert observation['network_rx_bps'] == 1234 and observation['network_tx_bps'] == 5678
    module.hub_network_sampler.cached = (module.time.monotonic()-40, payload)
    observation = client.get('/api/state', headers=ADMIN).json()['hub']['network']
    assert observation['network_status'] == 'stale'
    assert observation['network_rx_bps'] is observation['network_tx_bps'] is None
    assert observation['network_rx'] == 9999


def test_onedrive_settings_require_admin_and_same_origin_for_login(hub):
    _, client, _ = hub
    path = '/api/storage/onedrive'
    assert client.get(path).status_code == 401
    response = client.get(path, headers=ADMIN)
    assert response.status_code == 200
    assert response.json()['configured'] is False
    assert response.json()['cleanup']['scope'] == 'fleet_temp_only'
    assert response.headers['cache-control'] == 'no-store'
    data = {'client_id': '00000000-0000-0000-0000-000000000001'}
    assert client.post(path+'/login', headers=ADMIN, json=data).status_code == 403
    assert client.post(path+'/login', headers={**ADMIN, 'Origin': 'https://outside.test'}, json=data).status_code == 403


def test_onedrive_account_guard_permits_only_same_binding_when_busy(hub, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    module, _, _ = hub
    original = module.relay_transfers.provider
    settings = {'client_id': '00000000-0000-0000-0000-000000000001',
                'drive_id': 'synthetic-drive', 'folder_id': 'synthetic-folder'}
    module.relay_transfers.config = dict(settings)
    monkeypatch.setattr(module.relay_transfers, 'account_busy', lambda: True)

    async def scenario():
        with pytest.raises(HTTPException) as failure:
            async with module.onedrive_change_guard(dict(settings, drive_id='other-drive')):
                raise AssertionError('Account change must be rejected before activation')
        assert failure.value.detail == 'account_change_busy'
        assert module.relay_transfers.provider is original and not original._retired
        async with module.onedrive_change_guard(settings):
            module.activate_onedrive(settings)
        assert module.relay_transfers.provider is not original
        assert original._retired and module.CFG['onedrive'] == settings
        assert module.relay_transfers.storage_binding() == module.relay_transfers.storage_binding(settings)
        await module.relay_transfers.close()
    asyncio.run(scenario())


def test_web_onedrive_login_activates_running_hub_and_persists_configuration(hub):
    import asyncio
    import httpx
    module, _, root = hub
    previous = module.relay_transfers.provider
    seen = []
    client_id = '00000000-0000-0000-0000-000000000001'

    def microsoft(request):
        seen.append(request.url.path)
        if request.url.path.endswith('/devicecode'):
            return httpx.Response(200, json={'device_code': 'synthetic-device-code', 'user_code': 'TEST-CODE',
                'verification_uri': 'https://microsoft.com/devicelogin', 'expires_in': 900, 'interval': 1})
        if request.url.path.endswith('/token'):
            return httpx.Response(200, json={'access_token': 'synthetic-access',
                                           'refresh_token': 'synthetic-refresh', 'expires_in': 3600})
        assert request.headers['Authorization'] == 'Bearer synthetic-access'
        if request.url.path.endswith('/me/drive'):
            return httpx.Response(200, json={'id': 'synthetic-drive', 'driveType': 'personal',
                                           'quota': {'total': 20000000, 'remaining': 19000000}})
        if request.url.path.endswith('/root:/FleetHubTransfers'):
            return httpx.Response(200, json={'id': 'synthetic-folder', 'folder': {}})
        raise AssertionError('Unexpected Microsoft operation in isolated login test')

    async def scenario():
        async def immediate(_): await asyncio.sleep(0)
        module.onedrive_auth.sleep = immediate
        async with httpx.AsyncClient(transport=httpx.MockTransport(microsoft)) as remote:
            module.onedrive_auth.client = remote
            module.onedrive_auth.owns_client = False
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url='https://hub.test') as local:
                response = await local.post('/api/storage/onedrive/login',
                                            headers={**ADMIN, 'Origin': 'https://hub.test'}, json={'client_id': client_id})
                assert response.status_code == 200
                session_id = response.json()['session_id']
                await module.onedrive_auth.worker
                session = await local.get('/api/storage/onedrive/login/'+session_id, headers=ADMIN)
                assert session.json()['status'] == 'connected'
                state = (await local.get('/api/state', headers=ADMIN)).json()['storage']['onedrive']
                assert state['enabled'] is state['connected'] is True
                assert 'login' not in state and 'synthetic-refresh' not in json.dumps(state)
        assert previous._retired
        persisted = json.loads((root/'config.json').read_text())
        assert persisted['admin_token'] == 'synthetic-admin-token'
        assert persisted['onedrive']['drive_id'] == 'synthetic-drive'
        assert persisted['onedrive']['folder_id'] == 'synthetic-folder'
        assert (root/'onedrive-token.json').stat().st_mode & 0o777 == 0o600
        assert module.OneDrive(persisted['onedrive'], root/'onedrive-token.json').enabled
        assert len(seen) == 4
        await module.onedrive_auth.close()
        await module.relay_transfers.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('terminal', ['completed', 'failed'])
def test_transfer_terminal_metrics_survive_late_and_repeated_reports(transfer_record, terminal):
    module, client, transfer_id, token, _ = transfer_record
    url = '/api/transfers/' + transfer_id + '/progress'
    auth = {'Authorization': 'Bearer ' + token}
    final = {'status': terminal, 'phase': terminal, 'bytes_done': 100,
             'speed_bps': 10, 'elapsed_seconds': 190, 'transfer_seconds': 10,
             'error': 'synthetic failure' if terminal == 'failed' else ''}
    assert client.post(url, headers=auth, json=final).status_code == 200
    original = dict(module.db.execute('SELECT * FROM transfers WHERE id=?', (transfer_id,)).fetchone())
    for status in ('connecting', 'transferring', 'completed', 'failed'):
        assert client.post(url, headers=auth, json={
            'status': status, 'bytes_done': 1, 'speed_bps': 0,
            'elapsed_seconds': 999, 'transfer_seconds': 99, 'error': 'late',
        }).status_code == 200
        assert dict(module.db.execute('SELECT * FROM transfers WHERE id=?', (transfer_id,)).fetchone()) == original


def test_transfer_progress_rejects_regressions_and_recovers_from_stale(transfer_record):
    module, client, transfer_id, token, _ = transfer_record
    url = '/api/transfers/' + transfer_id + '/progress'
    auth = {'Authorization': 'Bearer ' + token}
    report = {'status': 'connecting', 'phase': 'connecting', 'bytes_done': 0,
              'speed_bps': 0, 'elapsed_seconds': 180, 'transfer_seconds': 0}
    assert client.post(url, headers=auth, json=report).status_code == 200
    assert client.post(url, headers=auth, json={**report, 'phase': 'preparing_tool', 'elapsed_seconds': 181}).status_code == 200
    assert module.db.execute('SELECT elapsed_seconds FROM transfers').fetchone()[0] == 180
    report.update(status='transferring', phase='transferring', bytes_done=50,
                  speed_bps=10, elapsed_seconds=185, transfer_seconds=5)
    assert client.post(url, headers=auth, json=report).status_code == 200
    original = dict(module.db.execute('SELECT * FROM transfers').fetchone())
    for late in ({'status': 'connecting', 'phase': 'connecting', 'elapsed_seconds': 186},
                 {'bytes_done': 49, 'elapsed_seconds': 186}, {'elapsed_seconds': 184}):
        assert client.post(url, headers=auth, json={**report, **late}).status_code == 200
        assert dict(module.db.execute('SELECT * FROM transfers').fetchone()) == original
    module.db.execute('UPDATE transfers SET progress_stale=1,speed_bps=0')
    module.db.commit()
    # Old clients have no phase or timing fields; preserve the existing timers.
    assert client.post(url, headers=auth, json={
        'status': 'transferring', 'bytes_done': 60, 'speed_bps': 10,
    }).status_code == 200
    stored = client.get('/api/state', headers=ADMIN).json()['transfers'][0]
    assert stored['progress_stale'] is False and stored['bytes_done'] == 60
    assert stored['elapsed_seconds'] == 185 and stored['transfer_seconds'] == 5


def test_delayed_request_body_cannot_overwrite_a_concurrent_completion(transfer_record):
    import asyncio
    import httpx
    module, _, transfer_id, token, _ = transfer_record
    url = '/api/transfers/' + transfer_id + '/progress'
    auth = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}

    async def scenario():
        reading, release = asyncio.Event(), asyncio.Event()
        async def delayed_body():
            reading.set()
            yield b'{"status":"transferring",'
            await release.wait()
            yield b'"bytes_done":1}'
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url='https://hub.test') as local:
            late = asyncio.create_task(local.post(url, headers=auth, content=delayed_body()))
            try:
                await asyncio.wait_for(reading.wait(), 2)
                assert (await local.post(url, headers=auth, json={
                    'status': 'completed', 'bytes_done': 100,
                })).status_code == 200
            finally:
                release.set()
                response = await asyncio.wait_for(late, 2)
            assert response.status_code == 200
    asyncio.run(scenario())
    row = module.db.execute('SELECT status,bytes_done FROM transfers').fetchone()
    assert tuple(row) == ('completed', 100)


@pytest.mark.parametrize('change', ['expire', 'revoke'])
def test_stale_transfer_keeps_only_unexpired_active_node_authorization(transfer_record, change):
    module, client, transfer_id, token, node_id = transfer_record
    module.db.execute('UPDATE transfers SET progress_stale=1')
    module.db.commit()
    hook = {'op': 'Login', 'content': {'user': transfer_id, 'client_id': transfer_id, 'metas': {'token': token}}}
    local = TestClient(module.app, client=('127.0.0.1', 4000))
    try:
        assert local.post('/internal/frp', json=hook).json()['reject'] is False
        if change == 'expire':
            module.db.execute('UPDATE transfers SET expires=?', (module.time.time()-1,))
            module.db.commit()
            state = client.get('/api/state', headers=ADMIN).json()['transfers'][0]
            assert state['authorization_expired'] is True and state['status'] == 'connecting'
        else:
            assert client.post('/api/nodes/' + node_id + '/revoke', headers=ADMIN).status_code == 200
        assert local.post('/internal/frp', json=hook).json()['reject'] is True
    finally:
        local.close()


def test_invalid_progress_cannot_change_transfer_record(transfer_record):
    module, client, transfer_id, token, _ = transfer_record
    url = '/api/transfers/' + transfer_id + '/progress'
    auth = {'Authorization': 'Bearer ' + token}
    original = dict(module.db.execute('SELECT * FROM transfers').fetchone())
    for invalid in ({'status': 'completed', 'phase': 'transferring'},
                    {'elapsed_seconds': 5, 'transfer_seconds': 6}, {'speed_bps': 'Infinity'},
                    {'bytes_done': 'invalid'}, {'elapsed_seconds': 10**400},
                    {'elapsed_seconds': 185}, {'transfer_seconds': 186}, []):
        assert client.post(url, headers=auth, json=invalid).status_code == 400
        assert dict(module.db.execute('SELECT * FROM transfers').fetchone()) == original


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
