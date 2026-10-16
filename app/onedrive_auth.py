"""Admin-only OneDrive device login. Tokens stay on the hub, never in responses."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from urllib.parse import quote, urlsplit
import uuid

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
import httpx

from onedrive import ProviderError


AUTH = 'https://login.microsoftonline.com/consumers/oauth2/v2.0'
GRAPH = 'https://graph.microsoft.com/v1.0'
SCOPE = 'offline_access Files.ReadWrite'
ACTIVE = ('pending', 'verifying')
JOURNAL = '.onedrive-auth-pending.json'
MAX_JSON = 1024 * 1024
ERROR_CODES = frozenset({
    'authorization_declined', 'authorization_expired', 'authorization_required',
    'invalid_device_response', 'invalid_token_response', 'device_authorization_failed',
    'provider_unavailable', 'provider_rejected', 'personal_drive_required',
    'folder_setup_failed', 'account_change_busy', 'configuration_failed', 'cancelled',
})


class LoginError(Exception):
    def __init__(self, code):
        self.code = code if code in ERROR_CODES else 'provider_unavailable'
        super().__init__(self.code)


def _read(path, limit=MAX_JSON):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise LoginError('configuration_failed')
    return value


def _json(path):
    value = json.loads(_read(path))
    if not isinstance(value, dict):
        raise LoginError('configuration_failed')
    return value


def _sync_directory(root):
    fd = os.open(root, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic(path, content):
    fd, temporary = tempfile.mkstemp(prefix='.onedrive-auth-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(temporary)


def _atomic_json(path, value):
    _atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())


def recover_pending(root):
    """Call before reading config at startup. Restore an interrupted two-file edit.

    The 0600 journal contains credentials and must never be served or logged.
    Recovery merges only the OneDrive section, retaining unrelated config edits.
    """
    root = Path(root)
    journal = root / JOURNAL
    if not journal.exists():
        return False
    try:
        saved = _json(journal)
        if (saved.get('version') != 1 or type(saved.get('had_onedrive')) is not bool
                or not isinstance(saved.get('old_onedrive'), (dict, type(None)))
                or not isinstance(saved.get('old_token'), (str, type(None)))):
            raise ValueError()
        config = _json(root / 'config.json')
        if saved['had_onedrive']:
            config['onedrive'] = saved['old_onedrive']
        else:
            config.pop('onedrive', None)
        if saved['old_token'] is None:
            (root / 'onedrive-token.json').unlink(missing_ok=True)
        else:
            old_token = base64.b64decode(saved['old_token'], validate=True)
            if len(old_token) > 65536:
                raise ValueError()
            _atomic(root / 'onedrive-token.json', old_token)
        _atomic_json(root / 'config.json', config)
        journal.unlink()
        _sync_directory(root)
        return True
    except (OSError, ValueError, UnicodeError, LoginError):
        raise LoginError('configuration_failed') from None


def commit_configuration(root, settings, refresh_token, activate):
    """Synchronous commit under the caller's transfer/refresh guard.

    activate(settings) must be a synchronous in-memory provider replacement,
    leaving the old provider intact if it raises. No network call belongs here.
    """
    root = Path(root)
    journal = root / JOURNAL
    saved = None
    try:
        if journal.exists():
            raise LoginError('configuration_failed')
        config = _json(root / 'config.json')
        if 'onedrive' in config and not isinstance(config['onedrive'], (dict, type(None))):
            raise LoginError('configuration_failed')
        token_path = root / 'onedrive-token.json'
        old_token = base64.b64encode(_read(token_path, 65536)).decode() if token_path.exists() else None
        saved = {'version': 1, 'had_onedrive': 'onedrive' in config,
                 'old_onedrive': config.get('onedrive'), 'old_token': old_token}
        _atomic_json(journal, saved)
        _atomic_json(token_path, {'refresh_token': refresh_token})
        config['onedrive'] = settings
        _atomic_json(root / 'config.json', config)
        journal.unlink()
        _sync_directory(root)
        # All fallible filesystem work precedes the synchronous memory switch.
        # A crash here restarts with the new, internally consistent disk pair.
        activate(settings)
    except Exception:
        try:
            if saved is not None and not journal.exists():
                _atomic_json(journal, saved)
            if journal.exists():
                recover_pending(root)
        except Exception:
            # Keep any surviving journal for startup recovery; never expose raw
            # filesystem or JSON errors (which can include credential content).
            raise LoginError('configuration_failed') from None
        raise LoginError('configuration_failed') from None


def _valid_token(value):
    return (isinstance(value, str) and 0 < len(value) <= 32768
            and not any(ord(c) < 33 or ord(c) == 127 for c in value))


def _quota(value):
    if (not isinstance(value, dict) or any(type(value.get(key)) is not int
            or not 0 <= value[key] <= 2**63 - 1 for key in ('total', 'remaining'))):
        return None
    return {key: value[key] for key in ('total', 'remaining')}


class OneDriveAuth:
    def __init__(self, config, root, *, get_provider, busy, change_guard, on_configured,
                 cleanup_status=lambda: {}, client=None, clock=time.monotonic,
                 wall_clock=time.time, sleep=asyncio.sleep):
        self.config, self.root = config, Path(root)
        self.get_provider, self.busy = get_provider, busy
        self.change_guard, self.on_configured = change_guard, on_configured
        self.cleanup_status = cleanup_status
        self.client, self.owns_client = client, client is None
        self.clock, self.wall_clock, self.sleep = clock, wall_clock, sleep
        self.lock, self.provider_lock = asyncio.Lock(), asyncio.Lock()
        self.login, self.worker = None, None
        self.connected, self.quota, self.error_code = False, None, None
        self.quota_checked = None

    def _settings(self):
        value = self.config.get('onedrive')
        return value if isinstance(value, dict) else {}

    def session(self, identifier=None):
        if not self.login or identifier is not None and self.login['session_id'] != identifier:
            raise HTTPException(404, 'login_not_found')
        result = {key: self.login[key] for key in ('session_id', 'status', 'expires_at', 'interval', 'error_code')}
        result.update({key: self.login[key] if self.login['status'] in ACTIVE else None
                       for key in ('user_code', 'verification_uri')})
        return result

    def snapshot(self):
        settings = self._settings()
        configured = all(isinstance(settings.get(key), str) and settings[key]
                         for key in ('client_id', 'drive_id', 'folder_id'))
        auth_state = ('connected' if self.connected else 'unconfigured' if not configured else
                      'authorization_required' if self.error_code == 'authorization_required' else 'unavailable')
        if self.login and self.login['status'] in ACTIVE:
            auth_state = self.login['status']
        return {'configured': bool(configured), 'connected': self.connected, 'auth_state': auth_state,
                'client_id': settings.get('client_id', ''), 'quota': self.quota,
                'error_code': self.error_code, 'can_switch_account': not self.busy(),
                'cleanup': self.cleanup_status(),
                'login': self.session() if self.login else None}

    async def status(self):
        async with self.provider_lock:
            provider = self.get_provider()
            if not provider.enabled:
                self.connected, self.quota, self.error_code = False, None, None
            elif (not self.login or self.login['status'] not in ACTIVE) and (
                    self.quota_checked is None or self.clock() - self.quota_checked >= 30):
                try:
                    async with asyncio.timeout(8):
                        quota = _quota(await provider.quota())
                    if quota is None:
                        raise LoginError('provider_unavailable')
                    self.connected, self.quota, self.error_code = True, quota, None
                except ProviderError as exc:
                    self.connected, self.quota = False, None
                    self.error_code = 'authorization_required' if exc.code == 'authorization_required' else 'provider_unavailable'
                except (Exception, asyncio.TimeoutError):
                    self.connected, self.quota, self.error_code = False, None, 'provider_unavailable'
                self.quota_checked = self.clock()
            return self.snapshot()

    async def _send(self, method, url, *, token=None, **kwargs):
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5, pool=3),
                                           follow_redirects=False, limits=httpx.Limits(max_connections=2))
        headers = {'Accept': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        try:
            async with asyncio.timeout(20):
                request = self.client.build_request(method, url, headers=headers, **kwargs)
                request.headers.pop('cookie', None)
                if not token:
                    request.headers.pop('authorization', None)
                response = await self.client.send(request, auth=None, follow_redirects=False, stream=True)
                try:
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > MAX_JSON:
                            raise LoginError('provider_rejected')
                        data.extend(chunk)
                    value = json.loads(data)
                    if not isinstance(value, dict):
                        raise LoginError('provider_rejected')
                    return response.status_code, value
                finally:
                    await response.aclose()
        except (httpx.HTTPError, TimeoutError):
            raise LoginError('provider_unavailable') from None
        except (ValueError, UnicodeError):
            raise LoginError('provider_rejected') from None

    async def start(self, client_id):
        try:
            if not isinstance(client_id, str):
                raise ValueError()
            client_id = str(uuid.UUID(client_id))
        except (ValueError, AttributeError):
            raise HTTPException(400, 'invalid_client_id') from None
        async with self.lock:
            if self.login and self.login['status'] in ACTIVE:
                raise HTTPException(409, 'login_in_progress')
            started = self.clock()
            try:
                status, device = await self._send('POST', AUTH + '/devicecode',
                                                data={'client_id': client_id, 'scope': SCOPE})
                if status != 200:
                    raise LoginError('device_authorization_failed')
                uri = device.get('verification_uri')
                try:
                    parsed = urlsplit(uri) if isinstance(uri, str) else None
                    valid_uri = (parsed and parsed.scheme == 'https' and parsed.hostname in {
                        'microsoft.com', 'www.microsoft.com', 'login.microsoftonline.com', 'aka.ms'}
                        and parsed.port in (None, 443) and not parsed.username and not parsed.password
                        and not parsed.fragment and not any(ord(c) < 33 or ord(c) == 127 for c in uri))
                except ValueError:
                    valid_uri = False
                user_code, device_code = device.get('user_code'), device.get('device_code')
                expires, interval = device.get('expires_in'), device.get('interval', 5)
                if (not valid_uri or not isinstance(user_code, str)
                        or not re.fullmatch('[A-Za-z0-9-]{4,32}', user_code) or not _valid_token(device_code)
                        or type(expires) is not int or not 0 < expires <= 86400
                        or type(interval) is not int or not 1 <= interval <= 60):
                    raise LoginError('invalid_device_response')
                lifetime = max(0, min(900, expires) - (self.clock() - started))
                if lifetime <= 0:
                    raise LoginError('authorization_expired')
            except LoginError as exc:
                raise HTTPException(502, exc.code) from None
            self.login = {'session_id': secrets.token_urlsafe(24), 'status': 'pending',
                          'client_id': client_id, 'user_code': user_code, 'verification_uri': uri,
                          'expires_at': self.wall_clock() + lifetime, 'interval': interval,
                          'error_code': None}
            self.worker = asyncio.create_task(self._run(self.login, device_code, self.clock() + lifetime))
            return self.session()

    async def _run(self, session, device_code, deadline):
        try:
            while self.clock() < deadline:
                await self.sleep(min(session['interval'], max(0, deadline - self.clock())))
                if self.clock() >= deadline:
                    break
                try:
                    async with asyncio.timeout(max(0.01, deadline - self.clock())):
                        status, value = await self._send('POST', AUTH + '/token', data={
                            'client_id': session['client_id'],
                            'grant_type': 'urn:ietf:params:oauth:grant-type:device_code', 'device_code': device_code})
                except LoginError as exc:
                    if exc.code != 'provider_unavailable':
                        raise
                    session['interval'] = min(60, session['interval'] + 5)
                    continue
                if status == 200:
                    if not all(_valid_token(value.get(key)) for key in ('access_token', 'refresh_token')):
                        raise LoginError('invalid_token_response')
                    session['status'] = 'verifying'
                    async with asyncio.timeout(60):
                        settings, quota = await self._folder(session['client_id'], value['access_token'])
                        async with self.provider_lock, self.change_guard(settings), self.lock:
                            # No await between the final identity check, durable
                            # writes and replacement of the running provider.
                            commit_configuration(self.root, settings, value['refresh_token'], self.on_configured)
                            self.config['onedrive'] = settings
                            self.connected, self.quota, self.error_code = True, quota, None
                            self.quota_checked = self.clock() if quota is not None else None
                            session['status'] = 'connected'
                    return
                error = value.get('error')
                if error == 'authorization_pending':
                    continue
                if error == 'slow_down' or status == 429 or status >= 500:
                    session['interval'] = min(60, session['interval'] + 5)
                    continue
                if error in ('authorization_declined', 'access_denied'):
                    raise LoginError('authorization_declined')
                if error in ('expired_token', 'bad_verification_code'):
                    raise LoginError('authorization_expired')
                raise LoginError('device_authorization_failed')
            raise LoginError('authorization_expired')
        except asyncio.CancelledError:
            if session['status'] in ACTIVE:
                session.update(status='cancelled', error_code='cancelled')
            raise
        except Exception as exc:
            code = (exc.code if isinstance(exc, LoginError) else 'account_change_busy'
                    if isinstance(exc, HTTPException) and exc.detail == 'account_change_busy' else
                    'authorization_expired' if isinstance(exc, TimeoutError) and session['status'] == 'pending'
                    else 'provider_unavailable')
            session.update(status='expired' if code == 'authorization_expired' else 'failed', error_code=code)
        finally:
            # Retain only UI-safe information after completion/cancellation.
            if session['status'] not in ACTIVE:
                session['user_code'] = session['verification_uri'] = None

    async def _folder(self, client_id, token):
        status, drive = await self._send('GET', GRAPH + '/me/drive', token=token,
                                         params={'$select': 'id,driveType,quota'})
        if status != 200:
            raise LoginError('provider_unavailable')
        if drive.get('driveType') != 'personal' or not _valid_token(drive.get('id')):
            raise LoginError('personal_drive_required')
        base = GRAPH + '/drives/' + quote(drive['id'], safe='')
        current = self._settings()
        if current.get('drive_id') == drive['id'] and isinstance(current.get('folder_id'), str) and current['folder_id']:
            status, folder = await self._send('GET', base + '/items/' + quote(current['folder_id'], safe=''),
                                              token=token, params={'$select': 'id,folder,remoteItem'})
        else:
            url = base + '/root:/FleetHubTransfers'
            status, folder = await self._send('GET', url, token=token, params={'$select': 'id,folder,remoteItem'})
            if status == 404:
                status, folder = await self._send('POST', base + '/root/children', token=token, json={
                    'name': 'FleetHubTransfers', 'folder': {}, '@microsoft.graph.conflictBehavior': 'fail'})
                if status == 409:
                    status, folder = await self._send('GET', url, token=token, params={'$select': 'id,folder,remoteItem'})
        if (status not in (200, 201) or not _valid_token(folder.get('id'))
                or not isinstance(folder.get('folder'), dict) or 'remoteItem' in folder):
            raise LoginError('folder_setup_failed')
        # Preserve transfer limits and other operator choices, replacing binding fields.
        settings = dict(current, client_id=client_id, tenant='consumers',
                        drive_id=drive['id'], folder_id=folder['id'])
        return settings, _quota(drive.get('quota'))

    async def cancel(self, identifier):
        async with self.lock:
            self.session(identifier)
            worker = self.worker
            if self.login['status'] in ACTIVE:
                self.login.update(status='cancelled', error_code='cancelled')
                if worker:
                    worker.cancel()
            result = self.session()
        if worker and worker.cancelled() is False and not worker.done():
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        return result

    async def close(self):
        if self.worker and not self.worker.done():
            if self.login and self.login['status'] in ACTIVE:
                self.login.update(status='cancelled', error_code='cancelled',
                                  user_code=None, verification_uri=None)
            self.worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.worker
        if self.owns_client and self.client is not None:
            await self.client.aclose()


def install_routes(app, manager, admin):
    parts = urlsplit(manager.config['base_url'])
    expected_origin = parts.scheme + '://' + parts.netloc

    def authorized(request, mutate=False):
        admin(request)
        if mutate and request.headers.get('origin') != expected_origin:
            raise HTTPException(403, 'origin_required')

    def response(value):
        return JSONResponse(value, headers={'Cache-Control': 'no-store', 'Pragma': 'no-cache',
                                            'Referrer-Policy': 'same-origin'})

    @app.get('/api/storage/onedrive')
    async def status(request: Request):
        authorized(request)
        return response(await manager.status())

    @app.post('/api/storage/onedrive/login')
    async def login(request: Request):
        authorized(request, True)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > 4096:
                raise HTTPException(413, 'invalid_json')
            body.extend(chunk)
        try:
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError()
        except (ValueError, UnicodeError):
            raise HTTPException(400, 'invalid_json') from None
        return response(await manager.start(value.get('client_id')))

    @app.get('/api/storage/onedrive/login/{identifier}')
    async def get_login(identifier: str, request: Request):
        authorized(request)
        return response(manager.session(identifier))

    @app.post('/api/storage/onedrive/login/{identifier}/cancel')
    async def cancel(identifier: str, request: Request):
        authorized(request, True)
        return response(await manager.cancel(identifier))
