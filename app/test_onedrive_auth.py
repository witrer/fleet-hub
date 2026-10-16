"""Real ASGI requests with synthetic OAuth/Graph HTTP; never contacts an account."""
import asyncio
import base64
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import onedrive_auth as auth
from onedrive import ProviderError

BASE = 'https://fleet.test'
HEADERS = {'Authorization': 'Bearer synthetic-admin', 'Origin': BASE}
CLIENT = '00000000-1111-2222-3333-444444444444'
DEVICE = {'device_code': 'synthetic-device-secret', 'user_code': 'TEST-CODE',
          'verification_uri': 'https://microsoft.com/devicelogin', 'interval': 2, 'expires_in': 900}
TOKENS = {'access_token': 'synthetic-access-secret', 'refresh_token': 'synthetic-refresh-secret'}
SETTINGS = {'client_id': CLIENT, 'tenant': 'consumers', 'drive_id': 'drive-id', 'folder_id': 'folder-id'}
QUOTA = {'total': 100_000_000_000, 'remaining': 20_000_000_000}


class Clock:
    def __init__(self, blocked=False):
        self.now, self.waits = 0, []
        self.blocked = blocked
        self.permit = asyncio.Event()

    def __call__(self):
        return self.now

    async def sleep(self, interval):
        self.waits.append(interval)
        if self.blocked:
            await self.permit.wait()
        self.now += interval
        await asyncio.sleep(0)


class Microsoft:
    def __init__(self):
        self.device = DEVICE.copy()
        self.responses = [(200, TOKENS)]
        self.drive = {'id': 'drive-id', 'driveType': 'personal', 'quota': QUOTA}
        self.folder = {'id': 'folder-id', 'folder': {}}
        self.folder_responses = []
        self.calls = []
        self.device_status = 200

    def __call__(self, request):
        self.calls.append(request)
        assert 'cookie' not in request.headers
        if request.url.host == 'login.microsoftonline.com':
            assert 'authorization' not in request.headers
            data = parse_qs(request.content.decode())
            assert data['client_id'] == [CLIENT]
            if request.url.path.endswith('/devicecode'):
                assert data['scope'] == ['offline_access Files.ReadWrite']
                return httpx.Response(self.device_status, json=self.device)
            assert request.url.path == '/consumers/oauth2/v2.0/token'
            assert data['device_code'] == [DEVICE['device_code']]
            assert data['grant_type'] == ['urn:ietf:params:oauth:grant-type:device_code']
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            status, data = response
            return httpx.Response(status, json=data)
        assert request.url.host == 'graph.microsoft.com'
        assert request.headers['authorization'] == 'Bearer ' + TOKENS['access_token']
        if request.url.path == '/v1.0/me/drive':
            return httpx.Response(200, json=self.drive)
        if self.folder_responses:
            status, data = self.folder_responses.pop(0)
            return httpx.Response(status, json=data)
        return httpx.Response(200, json=self.folder)


class Provider:
    def __init__(self, enabled):
        self.enabled = enabled
        self.calls = 0
        self.error = None
        self.entered, self.release = None, None

    async def quota(self):
        self.calls += 1
        if self.entered:
            self.entered.set()
            await self.release.wait()
        if self.error:
            raise self.error
        return QUOTA


@asynccontextmanager
async def harness(root, *, configured=False, blocked=False, microsoft=None):
    config = {'base_url': BASE, 'admin_token': 'synthetic-admin', 'unrelated': {'keep': True}}
    if configured:
        config['onedrive'] = dict(SETTINGS, max_active_tasks=3)
        (root / 'onedrive-token.json').write_text('{"refresh_token":"synthetic-old-refresh"}')
    (root / 'config.json').write_text(json.dumps(config))
    clock, microsoft = Clock(blocked), microsoft or Microsoft()
    state = {'busy': False, 'provider': Provider(configured), 'activations': []}
    create_lock, refresh_lock = asyncio.Lock(), asyncio.Lock()

    @asynccontextmanager
    async def guard(settings):
        async with create_lock, refresh_lock:
            current = config.get('onedrive', {})
            if state['busy'] and any(settings[key] != current.get(key) for key in ('drive_id', 'folder_id')):
                raise HTTPException(409, 'account_change_busy')
            yield

    def activate(settings):
        assert create_lock.locked() and refresh_lock.locked()
        assert json.loads((root / 'config.json').read_text())['onedrive'] == settings
        assert json.loads((root / 'onedrive-token.json').read_text())['refresh_token'] == TOKENS['refresh_token']
        assert not (root / auth.JOURNAL).exists()
        state['activations'].append(settings)
        state['provider'] = Provider(True)
        config['onedrive'] = settings

    async with httpx.AsyncClient(transport=httpx.MockTransport(microsoft),
            headers={'Cookie': 'synthetic-cookie-must-not-leak', 'Authorization': 'Bearer synthetic-stale'}) as upstream:
        manager = auth.OneDriveAuth(config, root, get_provider=lambda: state['provider'], busy=lambda: state['busy'],
            change_guard=guard, on_configured=activate, client=upstream, clock=clock, sleep=clock.sleep,
            wall_clock=lambda: 1700000000, cleanup_status=lambda: {'mode': 'permanent', 'pending_count': int(state['busy'])})
        app = FastAPI()
        def admin(request):
            if request.headers.get('authorization') != HEADERS['Authorization']:
                raise HTTPException(401, 'Authentication required')
        auth.install_routes(app, manager, admin)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=BASE) as client:
            yield manager, client, state, clock, microsoft, config
        await manager.close()


def start(client, **kwargs):
    return client.post('/api/storage/onedrive/login', headers=HEADERS, json={'client_id': CLIENT}, **kwargs)


def test_web_login_atomic_config_merge_and_no_token_leak(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (manager, client, state, clock, ms, config):
            response = await start(client)
            assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
            session = response.json()
            assert session['status'] == 'pending' and session['user_code'] == 'TEST-CODE'
            assert session['expires_at'] == 1700000900
            await manager.worker
            complete = (await client.get('/api/storage/onedrive/login/' + session['session_id'], headers=HEADERS)).json()
            assert complete['status'] == 'connected' and complete['user_code'] is None
            status = (await client.get('/api/storage/onedrive', headers=HEADERS)).json()
            assert status['connected'] and status['configured'] and status['quota'] == QUOTA
            assert status['can_switch_account'] and status['cleanup']['pending_count'] == 0
            saved = json.loads((tmp_path / 'config.json').read_text())
            assert saved['unrelated'] == {'keep': True} and saved['admin_token'] == 'synthetic-admin'
            assert saved['onedrive'] == SETTINGS and len(state['activations']) == 1
            for filename in ('config.json', 'onedrive-token.json'):
                assert (tmp_path / filename).stat().st_mode & 0o777 == 0o600
            assert not list(tmp_path.glob('.onedrive-auth-*'))
            exposed = json.dumps([session, complete, status])
            assert all(value not in exposed for value in (*TOKENS.values(), DEVICE['device_code'], 'synthetic-old-refresh'))
    asyncio.run(scenario())


def test_admin_and_origin_required_before_any_upstream_request(tmp_path):
    async def scenario():
        async with harness(tmp_path) as (_, client, _, _, ms, _):
            assert (await client.get('/api/storage/onedrive')).status_code == 401
            assert (await client.post('/api/storage/onedrive/login', json={'client_id': CLIENT})).status_code == 401
            for origin in (None, 'null', 'https://elsewhere.test'):
                headers = {'Authorization': HEADERS['Authorization']}
                if origin is not None:
                    headers['Origin'] = origin
                response = await client.post('/api/storage/onedrive/login', headers=headers, json={'client_id': CLIENT})
                assert response.status_code == 403 and response.json()['detail'] == 'origin_required'
            assert ms.calls == []
    asyncio.run(scenario())


def test_single_active_session_cancel_and_reload_recovery(tmp_path):
    async def scenario():
        async with harness(tmp_path, blocked=True) as (manager, client, _, _, ms, _):
            session = (await start(client)).json()
            response = await start(client)
            assert response.status_code == 409 and response.json()['detail'] == 'login_in_progress'
            status = (await client.get('/api/storage/onedrive', headers=HEADERS)).json()
            assert status['login']['session_id'] == session['session_id'] and status['auth_state'] == 'pending'
            path = '/api/storage/onedrive/login/' + session['session_id']
            assert (await client.post(path + '/cancel', headers={'Authorization': HEADERS['Authorization']})).status_code == 403
            assert (await client.get(path)).status_code == 401
            cancelled = (await client.post(path + '/cancel', headers=HEADERS)).json()
            assert cancelled['status'] == 'cancelled' and cancelled['verification_uri'] is None
            assert manager.worker.done() and len(ms.calls) == 1
            assert not (tmp_path / 'onedrive-token.json').exists()
    asyncio.run(scenario())


def test_pending_and_slow_down_respect_server_interval(tmp_path):
    async def scenario():
        ms = Microsoft()
        ms.responses = [(400, {'error': 'authorization_pending'}), (400, {'error': 'slow_down'}), (200, TOKENS)]
        async with harness(tmp_path, microsoft=ms) as (manager, client, _, clock, _, _):
            await start(client)
            await manager.worker
            assert manager.session()['status'] == 'connected' and clock.waits == [2, 2, 7]
    asyncio.run(scenario())


@pytest.mark.parametrize('oauth_error,expected', [('authorization_declined', 'failed'), ('expired_token', 'expired'),
                                               ('access_denied', 'failed'), ('invalid_client', 'failed')])
def test_denied_expired_and_oauth_errors_keep_old_credentials(tmp_path, oauth_error, expected):
    async def scenario():
        ms = Microsoft()
        ms.responses = [(400, {'error': oauth_error, 'error_description': 'synthetic-secret-do-not-return'})]
        async with harness(tmp_path, configured=True, microsoft=ms) as (manager, client, state, _, _, _):
            before = (tmp_path / 'onedrive-token.json').read_bytes()
            await start(client)
            await manager.worker
            assert manager.session()['status'] == expected and not state['activations']
            assert (tmp_path / 'onedrive-token.json').read_bytes() == before
            assert 'synthetic-secret' not in json.dumps(manager.snapshot())
    asyncio.run(scenario())


def test_deadline_prevents_late_token_poll(tmp_path):
    async def scenario():
        ms = Microsoft()
        ms.device.update(expires_in=1, interval=5)
        async with harness(tmp_path, microsoft=ms) as (manager, client, _, clock, ms, _):
            await start(client)
            await manager.worker
            assert manager.session()['status'] == 'expired' and clock.waits == [1]
            assert len(ms.calls) == 1
    asyncio.run(scenario())


def test_expiry_and_temporary_network_retry_are_bounded(tmp_path):
    async def scenario():
        ms = Microsoft()
        ms.responses = [httpx.ConnectError('synthetic-secret-connection'), (503, {}), (200, TOKENS)]
        async with harness(tmp_path, microsoft=ms) as (manager, client, _, clock, _, _):
            await start(client)
            await manager.worker
            assert manager.session()['status'] == 'connected' and clock.waits == [2, 7, 12]
    asyncio.run(scenario())


def test_busy_same_account_reauthorization_succeeds_with_existing_folder(tmp_path):
    async def scenario():
        async with harness(tmp_path, configured=True) as (manager, client, state, _, ms, _):
            state['busy'] = True
            await start(client)
            await manager.worker
            assert manager.session()['status'] == 'connected' and not manager.snapshot()['can_switch_account']
            assert ms.calls[-1].url.path == '/v1.0/drives/drive-id/items/folder-id'
            assert state['activations'][0]['max_active_tasks'] == 3
    asyncio.run(scenario())


def test_busy_account_switch_is_rejected_without_changing_files(tmp_path):
    async def scenario():
        ms = Microsoft()
        ms.drive['id'] = 'other-drive'
        async with harness(tmp_path, configured=True, microsoft=ms) as (manager, client, state, _, _, _):
            old_config, old_token = ((tmp_path / name).read_bytes() for name in ('config.json', 'onedrive-token.json'))
            state['busy'] = True
            assert (await start(client)).status_code == 200
            await manager.worker
            assert manager.session()['error_code'] == 'account_change_busy'
            assert not state['activations'] and (tmp_path / 'config.json').read_bytes() == old_config
            assert (tmp_path / 'onedrive-token.json').read_bytes() == old_token
    asyncio.run(scenario())


def test_task_started_after_login_is_checked_again_at_commit(tmp_path):
    async def scenario():
        async with harness(tmp_path, blocked=True) as (manager, client, state, clock, _, _):
            await start(client)
            state['busy'] = True
            clock.permit.set()
            await manager.worker
            assert manager.session()['error_code'] == 'account_change_busy'
            assert not state['activations'] and not (tmp_path / 'onedrive-token.json').exists()
    asyncio.run(scenario())


def test_folder_create_conflict_reuses_exact_folder(tmp_path):
    async def scenario():
        ms = Microsoft()
        ms.folder_responses = [(404, {}), (409, {}), (200, ms.folder)]
        async with harness(tmp_path, microsoft=ms) as (manager, client, _, _, _, _):
            await start(client)
            await manager.worker
            assert manager.session()['status'] == 'connected'
            assert [request.method for request in ms.calls[-3:]] == ['GET', 'POST', 'GET']
            assert json.loads(ms.calls[-2].content)['@microsoft.graph.conflictBehavior'] == 'fail'
    asyncio.run(scenario())


@pytest.mark.parametrize('bad_kind', ['business', 'remote_folder', 'missing_refresh', 'unsafe_uri', 'wrong_device_status'])
def test_invalid_provider_response_is_sanitized(tmp_path, bad_kind):
    async def scenario():
        ms = Microsoft()
        if bad_kind == 'business':
            ms.drive['driveType'] = 'business'
        elif bad_kind == 'remote_folder':
            ms.folder['remoteItem'] = {'synthetic': 'secret'}
        elif bad_kind == 'missing_refresh':
            ms.responses = [(200, {'access_token': TOKENS['access_token']})]
        elif bad_kind == 'unsafe_uri':
            ms.device['verification_uri'] = 'https://phishing.test/login'
        else:
            ms.device_status = 400
            ms.device['error_description'] = 'synthetic-secret-do-not-return'
        async with harness(tmp_path, microsoft=ms) as (manager, client, state, _, _, _):
            response = await start(client)
            if response.status_code == 200:
                await manager.worker
                assert manager.session()['status'] == 'failed'
            else:
                assert response.status_code == 502
            assert not state['activations'] and not (tmp_path / 'onedrive-token.json').exists()
            assert 'synthetic-secret' not in response.text
    asyncio.run(scenario())


def test_quota_is_cached_and_does_not_run_during_login(tmp_path):
    async def scenario():
        async with harness(tmp_path, configured=True, blocked=True) as (manager, client, state, clock, _, _):
            provider = state['provider']
            assert (await client.get('/api/storage/onedrive', headers=HEADERS)).json()['quota'] == QUOTA
            await client.get('/api/storage/onedrive', headers=HEADERS)
            assert provider.calls == 1
            clock.now += 31
            await start(client)
            await client.get('/api/storage/onedrive', headers=HEADERS)
            assert provider.calls == 1
            await manager.cancel(manager.session()['session_id'])
            await client.get('/api/storage/onedrive', headers=HEADERS)
            assert provider.calls == 2
    asyncio.run(scenario())


def test_quota_authorization_error_requests_reauthorization(tmp_path):
    async def scenario():
        async with harness(tmp_path, configured=True) as (_, client, state, _, _, _):
            state['provider'].error = ProviderError('authorization_required', 503)
            status = (await client.get('/api/storage/onedrive', headers=HEADERS)).json()
            assert status['auth_state'] == 'authorization_required' and status['connected'] is False
    asyncio.run(scenario())


def test_quota_and_provider_switch_do_not_overlap(tmp_path):
    async def scenario():
        async with harness(tmp_path, configured=True) as (manager, client, state, _, _, _):
            old = state['provider']
            old.entered, old.release = asyncio.Event(), asyncio.Event()
            status_task = asyncio.create_task(client.get('/api/storage/onedrive', headers=HEADERS))
            await old.entered.wait()
            await start(client)
            for _ in range(10):
                await asyncio.sleep(0)
            assert not state['activations']
            old.release.set()
            await status_task
            await manager.worker
            assert len(state['activations']) == 1
    asyncio.run(scenario())


def test_cancellation_does_not_wait_for_an_inflight_quota_request(tmp_path):
    async def scenario():
        async with harness(tmp_path, configured=True) as (manager, client, state, _, _, _):
            old = state['provider']
            old.entered, old.release = asyncio.Event(), asyncio.Event()
            status_task = asyncio.create_task(client.get('/api/storage/onedrive', headers=HEADERS))
            await old.entered.wait()
            session = (await start(client)).json()
            for _ in range(10):
                await asyncio.sleep(0)
            assert manager.session()['status'] == 'verifying'
            async with asyncio.timeout(1):
                result = await client.post('/api/storage/onedrive/login/' + session['session_id'] + '/cancel', headers=HEADERS)
            assert result.json()['status'] == 'cancelled' and not state['activations']
            old.release.set()
            await status_task
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['token', 'config', 'journal_unlink', 'final_fsync'])
def test_commit_failures_restore_files_and_never_activate(tmp_path, monkeypatch, stage):
    config = {'base_url': BASE, 'unrelated': 'preserve', 'onedrive': SETTINGS}
    old_token = b'{"refresh_token":"synthetic-old-refresh"}'
    (tmp_path / 'config.json').write_text(json.dumps(config))
    (tmp_path / 'onedrive-token.json').write_bytes(old_token)
    changed, tripped = [], False
    original_atomic, original_unlink, original_sync = auth._atomic, Path.unlink, auth._sync_directory

    def atomic(path, content):
        nonlocal tripped
        if not tripped and ((stage == 'token' and path.name == 'onedrive-token.json') or
                            (stage == 'config' and path.name == 'config.json')):
            tripped = True
            raise OSError('synthetic disk failure')
        return original_atomic(path, content)

    def unlink(path, *args, **kwargs):
        nonlocal tripped
        if stage == 'journal_unlink' and not tripped and path.name == auth.JOURNAL:
            tripped = True
            raise OSError('synthetic unlink failure')
        return original_unlink(path, *args, **kwargs)

    def sync(root):
        nonlocal tripped
        if stage == 'final_fsync' and not tripped and not (root / auth.JOURNAL).exists():
            tripped = True
            raise OSError('synthetic directory sync failure')
        return original_sync(root)

    monkeypatch.setattr(auth, '_atomic', atomic)
    monkeypatch.setattr(Path, 'unlink', unlink)
    monkeypatch.setattr(auth, '_sync_directory', sync)
    with pytest.raises(auth.LoginError, match='configuration_failed'):
        auth.commit_configuration(tmp_path, dict(SETTINGS, drive_id='new-drive'), 'synthetic-new-refresh', changed.append)
    assert tripped and not changed and json.loads((tmp_path / 'config.json').read_text()) == config
    assert (tmp_path / 'onedrive-token.json').read_bytes() == old_token
    assert not (tmp_path / auth.JOURNAL).exists()


def test_interrupted_configuration_recovers_before_startup(tmp_path):
    (tmp_path / 'config.json').write_text(json.dumps({'onedrive': {'drive_id': 'interrupted-new'}, 'unrelated': 'later-edit'}))
    (tmp_path / 'onedrive-token.json').write_text('{"refresh_token":"synthetic-new"}')
    old_token = b'{"refresh_token":"synthetic-old"}'
    auth._atomic_json(tmp_path / auth.JOURNAL, {'version': 1, 'had_onedrive': True,
        'old_onedrive': SETTINGS, 'old_token': base64.b64encode(old_token).decode()})
    assert auth.recover_pending(tmp_path) is True
    assert json.loads((tmp_path / 'config.json').read_text()) == {'onedrive': SETTINGS, 'unrelated': 'later-edit'}
    assert (tmp_path / 'onedrive-token.json').read_bytes() == old_token
    assert auth.recover_pending(tmp_path) is False


def test_first_authorization_recovery_removes_new_token_only(tmp_path):
    (tmp_path / 'config.json').write_text(json.dumps({'onedrive': SETTINGS, 'other': True}))
    (tmp_path / 'onedrive-token.json').write_text('{"refresh_token":"synthetic-new"}')
    auth._atomic_json(tmp_path / auth.JOURNAL, {'version': 1, 'had_onedrive': False,
        'old_onedrive': None, 'old_token': None})
    auth.recover_pending(tmp_path)
    assert json.loads((tmp_path / 'config.json').read_text()) == {'other': True}
    assert not (tmp_path / 'onedrive-token.json').exists()


def test_shutdown_cancels_background_login(tmp_path):
    async def scenario():
        async with harness(tmp_path, blocked=True) as (manager, client, _, _, _, _):
            await start(client)
            await manager.close()
            assert manager.worker.done()
            assert manager.session()['status'] == 'cancelled' and manager.session()['user_code'] is None
            assert not (tmp_path / 'onedrive-token.json').exists()
    asyncio.run(scenario())
