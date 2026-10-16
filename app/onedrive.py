"""OneDrive metadata adapter. File bytes travel directly between clients and Microsoft.

The operator supplies a delegated refresh token out of band; this module never
logs tokens, signed URLs, or upstream error bodies. Use one instance per hub.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import quote, urlsplit

import httpx

GRAPH = 'https://graph.microsoft.com/v1.0'
MAX_METADATA = 1024 * 1024


class ProviderError(Exception):
    def __init__(self, code, status=502, retry_after=None):
        self.code = code
        self.status = status
        self.retry_after = retry_after
        super().__init__(code)


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 2**63 - 1


def _https_url(value):
    try:
        parsed = urlsplit(value) if isinstance(value, str) else None
        if (not parsed or parsed.scheme != 'https' or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.fragment or parsed.port not in (None, 443)
                or any(ord(c) < 33 or ord(c) == 127 for c in value)):
            raise ValueError()
    except ValueError:
        raise ProviderError('invalid_provider_url') from None
    return value


def _name(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 255
            or value in ('.', '..') or value[-1] in ' .'
            or any(ord(c) < 32 or c in '"*:<>?/\\|' for c in value)):
        raise ProviderError('invalid_file_name', 400)
    return value


def _expiry(value):
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp.timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise ProviderError('invalid_provider_response') from None


def _retry_after(value):
    if not value or len(value) > 128:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        try:
            return max(0, int((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None


class OneDrive:
    provider_id = 'onedrive'
    # API/documentation capabilities, not a live account readiness check.
    cleanup_capabilities = {
        'bound_active_item_permanent_delete': True,
        'permanent_delete_if_match': 'undocumented',
        'historical_recycle_bin_cleanup': False,
        'immediate_quota_refresh': False,
    }

    def __init__(self, config, token_path, client=None):
        self.config = dict(config or {})
        self.token_path = Path(token_path)
        self._client = client
        self._owns_client = client is None
        self._refresh_lock = asyncio.Lock()
        self._access_token = None
        self._expires = 0
        self._retired = False

    @property
    def enabled(self):
        return all(isinstance(self.config.get(key), str) and self.config[key].strip()
                   for key in ('client_id', 'drive_id', 'folder_id'))

    def _configured(self):
        if self._retired:
            raise ProviderError('provider_reconfigured', 503)
        if not self.enabled:
            raise ProviderError('not_configured', 503)

    def retire(self):
        """Invalidate this instance while the caller holds its refresh lock.

        The account-switch path serializes credential replacement with refresh;
        queued refreshes must not overwrite the replacement account's token file.
        """
        self._retired = True
        self._access_token = None
        self._expires = 0

    def _http(self):
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(20, connect=5, pool=3),
                limits=httpx.Limits(max_connections=6, max_keepalive_connections=4),
                follow_redirects=False)
        return self._client

    async def close(self):
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _send(self, method, url, *, token=None, expected=(200,), headers=None, **kwargs):
        """Read bounded JSON only; redirects and rejected responses have no body read."""
        client = self._http()
        request = client.build_request(method, url, headers=headers, **kwargs)
        request.headers.pop('authorization', None)
        request.headers.pop('cookie', None)
        if token:
            request.headers['Authorization'] = 'Bearer ' + token
        try:
            async with asyncio.timeout(30):
                response = await client.send(request, auth=None, follow_redirects=False, stream=True)
                try:
                    status = response.status_code
                    if status not in expected:
                        codes = {401: 'authorization_required', 403: 'forbidden', 404: 'not_found',
                                 409: 'conflict', 412: 'item_changed', 429: 'throttled',
                                 507: 'quota_exceeded'}
                        raise ProviderError(codes.get(status, 'provider_unavailable' if status >= 500
                                                       else 'provider_rejected'), status if status >= 400 else 502,
                                            _retry_after(response.headers.get('Retry-After')))
                    if status in (204, 302):
                        return {}, response.headers
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_METADATA:
                            raise ProviderError('invalid_provider_response')
                    try:
                        data = json.loads(body)
                    except (ValueError, UnicodeError):
                        raise ProviderError('invalid_provider_response') from None
                    if not isinstance(data, dict):
                        raise ProviderError('invalid_provider_response')
                    return data, response.headers
                finally:
                    await response.aclose()
        except (httpx.HTTPError, TimeoutError, ValueError):
            raise ProviderError('provider_unavailable', 503) from None

    def _read_refresh(self):
        try:
            fd = os.open(self.token_path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'r', encoding='utf-8') as stream:
                data = json.loads(stream.read(65537))
            if not isinstance(data, dict):
                raise ValueError()
            token = data.get('refresh_token')
            if not isinstance(token, str) or not token or len(token) > 32768:
                raise ValueError()
            return data
        except (OSError, ValueError, UnicodeError):
            raise ProviderError('authorization_required', 503) from None

    def _save_refresh(self, token, previous):
        value = {'refresh_token': token}
        if previous.get('client_secret'):
            value['client_secret'] = previous['client_secret']
        name = None
        try:
            self.token_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.token_path.parent,
                                             prefix='.onedrive-token-', delete=False) as stream:
                name = stream.name
                os.fchmod(stream.fileno(), 0o600)
                json.dump(value, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.token_path)
        except (OSError, TypeError, ValueError):
            raise ProviderError('token_storage_unavailable', 503) from None
        finally:
            if name:
                try:
                    os.unlink(name)
                except OSError:
                    pass

    async def _token(self, rejected=None):
        self._configured()
        async with self._refresh_lock:
            self._configured()
            if (self._access_token and self._access_token != rejected
                    and self._expires > time.time() + 60):
                return self._access_token
            saved = self._read_refresh()
            tenant = self.config.get('tenant', 'consumers')
            if not isinstance(tenant, str) or not re.fullmatch(r'[A-Za-z0-9.-]+', tenant):
                raise ProviderError('invalid_configuration', 503)
            form = {'client_id': self.config['client_id'], 'grant_type': 'refresh_token',
                    'refresh_token': saved['refresh_token'],
                    'scope': 'https://graph.microsoft.com/Files.ReadWrite offline_access'}
            if saved.get('client_secret'):
                form['client_secret'] = saved['client_secret']
            try:
                data, _ = await self._send('POST', 'https://login.microsoftonline.com/' + tenant +
                                          '/oauth2/v2.0/token', data=form)
            except ProviderError as exc:
                if exc.status in (400, 401, 403):
                    raise ProviderError('authorization_required', 503) from None
                raise
            token = data.get('access_token')
            refresh = data.get('refresh_token', saved['refresh_token'])
            lifetime = data.get('expires_in')
            if (not isinstance(token, str) or not token or len(token) > 32768
                    or any(ord(c) < 33 or ord(c) == 127 for c in token)
                    or not isinstance(refresh, str) or not refresh or len(refresh) > 32768
                    or not _integer(lifetime) or lifetime <= 0):
                raise ProviderError('invalid_provider_response')
            # Persist rotated credentials before treating the refresh as successful.
            self._save_refresh(refresh, saved)
            self._access_token = token
            self._expires = time.time() + lifetime
            return token

    async def _graph(self, method, path, **kwargs):
        token = await self._token()
        for attempt in range(2):
            try:
                return await self._send(method, GRAPH + path, token=token, **kwargs)
            except ProviderError as exc:
                if exc.status != 401 or attempt:
                    raise
                token = await self._token(rejected=token)

    def _drive(self):
        self._configured()
        return '/drives/' + quote(self.config['drive_id'], safe='')

    def _item_path(self, item_id):
        if not isinstance(item_id, str) or not item_id or len(item_id) > 1024:
            raise ProviderError('invalid_item_id', 400)
        return self._drive() + '/items/' + quote(item_id, safe='')

    def _child_path(self, name):
        return self._item_path(self.config['folder_id']) + ':/' + quote(_name(name), safe='')

    async def quota(self):
        data, _ = await self._graph('GET', self._drive(), params={'$select': 'quota'})
        quota = data.get('quota', {})
        if (not isinstance(quota, dict) or not _integer(quota.get('remaining'))
                or not _integer(quota.get('total'))):
            raise ProviderError('invalid_provider_response')
        return {'remaining': quota['remaining'], 'total': quota['total']}

    async def create_upload(self, name, size):
        name = _name(name)
        if not _integer(size) or size == 0:
            raise ProviderError('invalid_file_size', 400)
        # Personal OneDrive can reject item.fileSize. Total bytes are sent in
        # each upload PUT's Content-Range and checked against the saved manifest.
        data, _ = await self._graph('POST', self._child_path(name) + ':/createUploadSession',
                                   json={'item': {'@microsoft.graph.conflictBehavior': 'fail',
                                                  'name': name}})
        return {'upload_url': _https_url(data.get('uploadUrl')),
                'expires_at': _expiry(data.get('expirationDateTime'))}

    async def _metadata(self, path, *, expected_id=None, expected_name=None, download=True):
        data, _ = await self._graph('GET', path, params={'$select':
            'id,name,size,eTag,parentReference,file,folder,remoteItem,@microsoft.graph.downloadUrl'})
        return await self._checked_item(data, expected_id=expected_id,
                                        expected_name=expected_name, download=download)

    async def _checked_item(self, data, *, expected_id=None, expected_name=None, download=True):
        parent = data.get('parentReference')
        if (not isinstance(parent, dict) or parent.get('id') != self.config['folder_id']
                or parent.get('driveId', self.config['drive_id']) != self.config['drive_id']
                or not isinstance(data.get('file'), dict) or 'folder' in data or 'remoteItem' in data):
            raise ProviderError('item_outside_staging', 403)
        item_id, name, etag = data.get('id'), data.get('name'), data.get('eTag')
        if (not isinstance(item_id, str) or not item_id or len(item_id) > 1024
                or not isinstance(name, str) or not name or len(name) > 255
                or not isinstance(etag, str) or not etag or len(etag) > 2048
                or not _integer(data.get('size'))
                or (expected_id is not None and item_id != expected_id)
                or (expected_name is not None and name != expected_name)):
            raise ProviderError('invalid_provider_response')
        result = {'id': item_id, 'name': name, 'size': data['size'], 'etag': etag}
        if download:
            url = data.get('@microsoft.graph.downloadUrl')
            if not url:
                _, headers = await self._graph('GET', self._item_path(item_id) + '/content', expected=(302,))
                url = headers.get('Location')
            result['download_url'] = _https_url(url)
        return result

    async def item(self, item_id):
        return await self._metadata(self._item_path(item_id), expected_id=item_id)

    async def find_item(self, name):
        return await self._metadata(self._child_path(name), expected_name=name)

    async def create_empty(self, name):
        name = _name(name)
        data, _ = await self._graph('PUT', self._child_path(name) + ':/content',
                                   expected=(200, 201), content=b'',
                                   params={'@microsoft.graph.conflictBehavior': 'fail'},
                                   headers={'Content-Type': 'application/octet-stream'})
        result = await self._checked_item(data, expected_name=name)
        if result['size'] != 0:
            raise ProviderError('invalid_provider_response')
        return result

    async def delete(self, item_id, expected_name=None, expected_etag=None):
        metadata = await self._metadata(self._item_path(item_id), expected_id=item_id, download=False)
        if ((expected_name is not None and metadata['name'] != expected_name)
                or (expected_etag is not None and metadata['etag'] != expected_etag)):
            raise ProviderError('item_changed', 412)
        await self._graph('DELETE', self._item_path(item_id), expected=(204,),
                          headers={'If-Match': expected_etag if expected_etag is not None else metadata['etag']})

    async def permanent_delete(self, item_id, *, expected_name, expected_etag):
        """Purge one terminal Fleet task's bound, still-active staging file.

        Never enumerate or empty the account's recycle bin. Microsoft documents
        this action for personal accounts, but does not document If-Match support
        on it: the header below is defensive, not an atomicity guarantee. Callers
        must only use task-bound IDs, serialize task cleanup, and retain failures
        for retry/review; a 404 does not prove that quota has been released.
        """
        if (not isinstance(expected_name, str) or not expected_name
                or not isinstance(expected_etag, str) or not expected_etag
                or len(expected_etag) > 2048
                or any(ord(c) < 32 or ord(c) == 127 for c in expected_etag)):
            raise ProviderError('cleanup_binding_required', 409)
        _name(expected_name)
        path = self._item_path(item_id)
        try:
            metadata = await self._metadata(path, expected_id=item_id, download=False)
            if metadata['name'] != expected_name or metadata['etag'] != expected_etag:
                raise ProviderError('item_changed', 412)
            await self._graph('POST', path + '/permanentDelete', expected=(204,),
                              headers={'Accept': 'application/json', 'If-Match': expected_etag})
        except ProviderError as exc:
            if exc.status == 404:
                # Missing active metadata includes already-recycled items. Do not
                # claim they are purged or broaden cleanup to unbound objects.
                raise ProviderError('cleanup_item_unavailable', 404) from None
            if exc.status in (405, 501):
                raise ProviderError('permanent_delete_unsupported', exc.status) from None
            raise

    async def upload_status(self, upload_url):
        # Only pass URLs previously returned by create_upload and held in server state.
        data, _ = await self._send('GET', _https_url(upload_url))
        ranges = data.get('nextExpectedRanges')
        if (not isinstance(ranges, list) or not ranges or len(ranges) > 1000
                or any(not isinstance(r, str) or not re.fullmatch(r'[0-9]{1,20}-[0-9]{0,20}', r)
                       for r in ranges)):
            raise ProviderError('invalid_provider_response')
        return {'expires_at': _expiry(data.get('expirationDateTime')),
                'next_expected_ranges': ranges}

    async def cancel_upload(self, upload_url):
        await self._send('DELETE', _https_url(upload_url), expected=(204,))
