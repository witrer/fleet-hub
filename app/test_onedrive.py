"""OneDrive contract checks with synthetic metadata and no account/network access."""
import asyncio
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from onedrive import OneDrive, ProviderError

CONFIG = {'client_id': 'test-app', 'drive_id': 'test-drive', 'folder_id': 'test-folder'}
UPLOAD = 'https://sn3302.up.1drv.com/up/test-session?auth=synthetic'
DOWNLOAD = 'https://files.1drv.com/test-item?auth=synthetic'
EXPIRY = '2030-01-01T00:00:00Z'


def metadata(**overrides):
    result = {'id': 'test-item', 'name': 'test-name', 'size': 10,
              'eTag': '"test-etag"', 'parentReference': {'id': 'test-folder', 'driveId': 'test-drive'},
              'file': {}, '@microsoft.graph.downloadUrl': DOWNLOAD}
    result.update(overrides)
    return result


@pytest.fixture
def provider(tmp_path):
    instances = []
    def make(handler):
        token_path = tmp_path / 'synthetic-token.json'
        token_path.write_text(json.dumps({'refresh_token': 'synthetic-old-refresh'}))
        async def route(request):
            if request.url.host == 'login.microsoftonline.com':
                assert request.url.path == '/consumers/oauth2/v2.0/token'
                assert 'authorization' not in request.headers
                form = parse_qs(request.content.decode())
                assert form['grant_type'] == ['refresh_token']
                assert form['scope'] == ['https://graph.microsoft.com/Files.ReadWrite offline_access']
                return httpx.Response(200, json={'access_token': 'synthetic-access',
                    'refresh_token': 'synthetic-new-refresh', 'expires_in': 3600})
            if request.url.host == 'graph.microsoft.com':
                assert request.headers['authorization'] == 'Bearer synthetic-access'
            result = handler(request)
            return await result if hasattr(result, '__await__') else result
        client = httpx.AsyncClient(transport=httpx.MockTransport(route),
                                  headers={'Authorization': 'Bearer must-not-leak', 'Cookie': 'must-not-leak'})
        item = OneDrive(CONFIG, token_path, client=client)
        instances.append(client)
        return item
    yield make
    for client in instances:
        asyncio.run(client.aclose())


@pytest.mark.parametrize('size', [10, 499 * 1024**2])
def test_upload_contract_and_refresh_persistence(provider, size):
    calls = []
    def handle(request):
        calls.append(request)
        assert request.method == 'POST'
        assert request.url.path == '/v1.0/drives/test-drive/items/test-folder:/test-name:/createUploadSession'
        payload = json.loads(request.content)
        # Reproduce the reported personal OneDrive rejection before returning a session.
        if 'fileSize' in payload.get('item', {}):
            return httpx.Response(400, json={'error': {'code': 'invalidRequest'}})
        assert payload == {'item': {'@microsoft.graph.conflictBehavior': 'fail',
                                    'name': 'test-name'}}
        return httpx.Response(200, json={'uploadUrl': UPLOAD, 'expirationDateTime': EXPIRY})
    item = provider(handle)
    result = asyncio.run(item.create_upload('test-name', size))
    assert result == {'upload_url': UPLOAD, 'expires_at': 1893456000.0}
    assert json.loads(item.token_path.read_text()) == {'refresh_token': 'synthetic-new-refresh'}
    assert item.token_path.stat().st_mode & 0o777 == 0o600
    assert list(item.token_path.parent.glob('.onedrive-token-*')) == []
    assert item.enabled and item.provider_id == 'onedrive' and len(calls) == 1


def test_concurrent_calls_only_refresh_once(tmp_path):
    token_path = tmp_path / 'synthetic-token.json'
    token_path.write_text('{"refresh_token":"synthetic-refresh"}')
    refreshes = []
    async def handle(request):
        if request.url.host == 'login.microsoftonline.com':
            refreshes.append(request)
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={'access_token': 'synthetic-token', 'expires_in': 3600})
        return httpx.Response(200, json={'quota': {'total': 100, 'remaining': 90}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            item = OneDrive(CONFIG, token_path, client=client)
            result = await asyncio.gather(*(item.quota() for _ in range(8)))
            assert result == [{'total': 100, 'remaining': 90}] * 8
    asyncio.run(run())
    assert len(refreshes) == 1


def test_retired_provider_cannot_refresh_or_overwrite_replacement_account_token(provider):
    item = provider(lambda request: pytest.fail('retired provider must not make HTTP requests'))
    async def run():
        await item._refresh_lock.acquire()
        pending = asyncio.create_task(item.quota())
        # Let the old request pass its initial configuration check and queue for
        # the lock held by the account replacement path.
        await asyncio.sleep(0)
        assert not pending.done()
        replacement = '{"refresh_token":"synthetic-replacement-account"}'
        item.token_path.write_text(replacement)
        item._access_token = 'synthetic-old-access'
        item._expires = 2**40
        item.retire()
        item._refresh_lock.release()
        with pytest.raises(ProviderError, match='provider_reconfigured') as raised:
            await pending
        assert raised.value.status == 503
        assert item.token_path.read_text() == replacement
        assert item._access_token is None and item._expires == 0
        with pytest.raises(ProviderError, match='provider_reconfigured'):
            await item.quota()
    asyncio.run(run())


def test_upload_status_and_cancel_do_not_forward_graph_or_client_auth(provider):
    calls = []
    def handle(request):
        calls.append(request)
        assert str(request.url) == UPLOAD
        assert 'authorization' not in request.headers and 'cookie' not in request.headers
        if request.method == 'DELETE':
            return httpx.Response(204)
        return httpx.Response(200, json={'expirationDateTime': EXPIRY, 'nextExpectedRanges': ['327680-']})
    item = provider(handle)
    async def run():
        assert await item.upload_status(UPLOAD) == {
            'expires_at': 1893456000.0, 'next_expected_ranges': ['327680-']}
        await item.cancel_upload(UPLOAD)
    asyncio.run(run())
    assert [r.method for r in calls] == ['GET', 'DELETE']


def test_item_returns_only_safe_fields_and_refreshes_download_url(provider):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=metadata(**{'webUrl': 'private-web-page', 'owner': 'private-owner'}))
    item = provider(handle)
    async def run():
        for _ in range(2):
            result = await item.item('test-item')
            assert result == {'id': 'test-item', 'name': 'test-name', 'size': 10,
                              'etag': '"test-etag"', 'download_url': DOWNLOAD}
    asyncio.run(run())
    assert len(calls) == 2


def test_content_redirect_is_returned_without_following_or_reading_bytes(provider):
    calls = []
    class NeverRead(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError('hub must not read file content')
            yield b''
    def handle(request):
        calls.append(request)
        if request.url.path.endswith('/content'):
            return httpx.Response(302, headers={'Location': DOWNLOAD}, stream=NeverRead())
        data = metadata()
        del data['@microsoft.graph.downloadUrl']
        return httpx.Response(200, json=data)
    result = asyncio.run(provider(handle).item('test-item'))
    assert result['download_url'] == DOWNLOAD
    assert len(calls) == 2 and all(r.url.host == 'graph.microsoft.com' for r in calls)


def test_content_unexpected_200_does_not_proxy_file_bytes(provider):
    class NeverRead(httpx.AsyncByteStream):
        async def __aiter__(self):
            raise AssertionError('hub must not read file content')
            yield b''
    def handle(request):
        if request.url.path.endswith('/content'):
            return httpx.Response(200, stream=NeverRead())
        data = metadata()
        del data['@microsoft.graph.downloadUrl']
        return httpx.Response(200, json=data)
    with pytest.raises(ProviderError, match='provider_rejected'):
        asyncio.run(provider(handle).item('test-item'))


@pytest.mark.parametrize('change', [
    {'parentReference': {'id': 'another-folder', 'driveId': 'test-drive'}},
    {'parentReference': {'id': 'test-folder', 'driveId': 'another-drive'}},
    {'folder': {}}, {'remoteItem': {}}, {'file': None},
])
def test_item_scope_is_checked_before_download_or_delete(provider, change):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=metadata(**change))
    item = provider(handle)
    with pytest.raises(ProviderError, match='item_outside_staging'):
        asyncio.run(item.delete('test-item'))
    assert len(calls) == 1 and calls[0].method == 'GET'


def test_delete_requires_matching_etag_and_never_resolves_download(provider):
    calls = []
    def handle(request):
        calls.append(request)
        if request.method == 'DELETE':
            assert request.headers['if-match'] == '"test-etag"'
            return httpx.Response(204)
        data = metadata()
        del data['@microsoft.graph.downloadUrl']
        return httpx.Response(200, json=data)
    asyncio.run(provider(handle).delete('test-item'))
    assert [r.method for r in calls] == ['GET', 'DELETE']


@pytest.mark.parametrize('change', [{'name': 'externally-renamed'}, {'eTag': '"externally-replaced"'}])
def test_cleanup_refuses_task_item_modified_after_binding(provider, change):
    calls = []
    def handle(request):
        calls.append(request)
        assert request.method == 'GET', 'must not delete externally modified task content'
        return httpx.Response(200, json=metadata(**change))
    with pytest.raises(ProviderError, match='item_changed') as raised:
        asyncio.run(provider(handle).delete('test-item', expected_name='test-name',
                                            expected_etag='"test-etag"'))
    assert raised.value.status == 412 and len(calls) == 1


def test_cleanup_sends_original_bound_etag(provider):
    calls = []
    def handle(request):
        calls.append(request)
        if request.method == 'DELETE':
            assert request.headers['if-match'] == '"test-etag"'
            return httpx.Response(204)
        return httpx.Response(200, json=metadata())
    asyncio.run(provider(handle).delete('test-item', expected_name='test-name',
                                        expected_etag='"test-etag"'))
    assert [r.method for r in calls] == ['GET', 'DELETE']


def test_permanent_cleanup_only_purges_the_bound_active_file(provider):
    calls = []
    def handle(request):
        calls.append(request)
        if request.method == 'POST':
            assert request.url.path == '/v1.0/drives/test-drive/items/test-item/permanentDelete'
            assert request.headers['if-match'] == '"test-etag"'
            assert request.headers['accept'] == 'application/json'
            assert request.content == b''
            return httpx.Response(204)
        data = metadata()
        del data['@microsoft.graph.downloadUrl']
        return httpx.Response(200, json=data)
    item = provider(handle)
    asyncio.run(item.permanent_delete('test-item', expected_name='test-name', expected_etag='"test-etag"'))
    assert [r.method for r in calls] == ['GET', 'POST']
    assert item.cleanup_capabilities == {
        'bound_active_item_permanent_delete': True,
        'permanent_delete_if_match': 'undocumented',
        'historical_recycle_bin_cleanup': False,
        'immediate_quota_refresh': False,
    }


@pytest.mark.parametrize('change,code', [
    ({'name': 'externally-renamed'}, 'item_changed'),
    ({'eTag': '"externally-replaced"'}, 'item_changed'),
    ({'parentReference': {'id': 'another-folder', 'driveId': 'test-drive'}}, 'item_outside_staging'),
    ({'parentReference': {'id': 'test-folder', 'driveId': 'another-drive'}}, 'item_outside_staging'),
    ({'folder': {}}, 'item_outside_staging'),
])
def test_permanent_cleanup_refuses_changed_or_unbound_objects(provider, change, code):
    calls = []
    def handle(request):
        calls.append(request)
        assert request.method == 'GET', 'must not purge changed or out-of-scope files'
        return httpx.Response(200, json=metadata(**change))
    with pytest.raises(ProviderError, match=code):
        asyncio.run(provider(handle).permanent_delete('test-item', expected_name='test-name',
                                                      expected_etag='"test-etag"'))
    assert len(calls) == 1


@pytest.mark.parametrize('name,etag', [(None, '"tag"'), ('', '"tag"'), ('name', None),
                                    ('name', ''), ('name', '"tag"\r\nother: header')])
def test_permanent_cleanup_requires_complete_original_binding(provider, name, etag):
    item = provider(lambda request: pytest.fail('incomplete cleanup binding must fail locally'))
    with pytest.raises(ProviderError, match='cleanup_binding_required'):
        asyncio.run(item.permanent_delete('test-item', expected_name=name, expected_etag=etag))


@pytest.mark.parametrize('stage', ['metadata', 'action'])
def test_missing_active_item_does_not_claim_historical_recycle_bin_was_purged(provider, stage):
    calls = []
    def handle(request):
        calls.append(request)
        if stage == 'metadata' or request.method == 'POST':
            return httpx.Response(404, text='private Microsoft details')
        return httpx.Response(200, json=metadata())
    with pytest.raises(ProviderError, match='cleanup_item_unavailable') as raised:
        asyncio.run(provider(handle).permanent_delete('test-item', expected_name='test-name',
                                                      expected_etag='"test-etag"'))
    assert raised.value.status == 404
    assert [r.method for r in calls] == (['GET'] if stage == 'metadata' else ['GET', 'POST'])


@pytest.mark.parametrize('status,code', [(405, 'permanent_delete_unsupported'),
                                       (501, 'permanent_delete_unsupported'), (412, 'item_changed'),
                                       (429, 'throttled'), (403, 'forbidden')])
def test_permanent_cleanup_failure_never_falls_back_to_recycle_or_unconditional_delete(provider, status, code):
    calls = []
    def handle(request):
        calls.append(request)
        if request.method == 'POST':
            return httpx.Response(status, headers={'Retry-After': '19'}, text='private Microsoft details')
        return httpx.Response(200, json=metadata())
    with pytest.raises(ProviderError, match=code) as raised:
        asyncio.run(provider(handle).permanent_delete('test-item', expected_name='test-name',
                                                      expected_etag='"test-etag"'))
    assert [r.method for r in calls] == ['GET', 'POST']
    if status == 429:
        assert raised.value.retry_after == 19


def test_find_recovers_completed_upload_by_generated_name(provider):
    def handle(request):
        assert request.url.path.endswith('/items/test-folder:/test-name')
        return httpx.Response(200, json=metadata())
    assert asyncio.run(provider(handle).find_item('test-name'))['id'] == 'test-item'


@pytest.mark.parametrize('size', [0, 10])
def test_find_needs_metadata_only_even_without_download_url(provider, size):
    calls = []
    def handle(request):
        calls.append(request)
        assert request.url.path.endswith('/items/test-folder:/test-name')
        data = metadata(size=size)
        del data['@microsoft.graph.downloadUrl']
        return httpx.Response(200, json=data)
    result = asyncio.run(provider(handle).find_item('test-name'))
    assert result == {'id': 'test-item', 'name': 'test-name', 'size': size, 'etag': '"test-etag"'}
    assert len(calls) == 1


def test_empty_file_has_no_bytes_and_does_not_replace_existing_item(provider):
    def handle(request):
        assert request.method == 'PUT' and request.content == b''
        assert request.url.params['@microsoft.graph.conflictBehavior'] == 'fail'
        return httpx.Response(201, json=metadata(size=0))
    assert asyncio.run(provider(handle).create_empty('test-name'))['size'] == 0


@pytest.mark.parametrize('url', ['http://files.1drv.com/a', 'https://user:pass@files.1drv.com/a',
                               'https://files.1drv.com/a#fragment', 'https://files.1drv.com:444/a',
                               'https://files.1drv.com/a\n', 'file:///etc/passwd'])
def test_invalid_capability_urls_rejected_without_request(provider, url):
    item = provider(lambda request: pytest.fail('must not send invalid URL'))
    with pytest.raises(ProviderError, match='invalid_provider_url'):
        asyncio.run(item.upload_status(url))


@pytest.mark.parametrize('status,code', [(429, 'throttled'), (507, 'quota_exceeded'),
                                       (404, 'not_found'), (403, 'forbidden'), (503, 'provider_unavailable')])
def test_errors_redact_raw_provider_body_and_keep_retry_after(provider, status, code):
    def handle(request):
        return httpx.Response(status, headers={'Retry-After': '17'}, text='SECRET URL=' + UPLOAD)
    with pytest.raises(ProviderError) as raised:
        asyncio.run(provider(handle).quota())
    assert str(raised.value) == code and raised.value.status == status and raised.value.retry_after == 17
    assert raised.value.__cause__ is None


def test_graph_401_refreshes_once_and_retries_with_rotated_token(tmp_path):
    token_path = tmp_path / 'synthetic-token.json'
    token_path.write_text('{"refresh_token":"refresh-0"}')
    refreshes, graphs = [], []
    def handle(request):
        if request.url.host == 'login.microsoftonline.com':
            form = parse_qs(request.content.decode())
            assert form['refresh_token'] == ['refresh-' + str(len(refreshes))]
            refreshes.append(request)
            return httpx.Response(200, json={'access_token': 'access-' + str(len(refreshes)),
                'refresh_token': 'refresh-' + str(len(refreshes)), 'expires_in': 3600})
        graphs.append(request.headers['authorization'])
        if len(graphs) == 1:
            return httpx.Response(401, text='private details')
        return httpx.Response(200, json={'quota': {'total': 100, 'remaining': 90}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            assert await OneDrive(CONFIG, token_path, client=client).quota() == {'total': 100, 'remaining': 90}
    asyncio.run(run())
    assert graphs == ['Bearer access-1', 'Bearer access-2'] and len(refreshes) == 2


@pytest.mark.parametrize('name', ['../escape', 'a/b', 'a\\b', '..', '.', 'name.', 'name ', 'bad:name'])
def test_remote_names_cannot_escape_staging_folder(provider, name):
    item = provider(lambda request: pytest.fail('invalid name must fail locally'))
    with pytest.raises(ProviderError, match='invalid_file_name'):
        asyncio.run(item.create_upload(name, 10))


def test_absent_configuration_and_credentials_fail_safely(tmp_path):
    missing = OneDrive({}, tmp_path / 'missing.json')
    assert missing.enabled is False
    with pytest.raises(ProviderError, match='not_configured'):
        asyncio.run(missing.quota())
    configured = OneDrive(CONFIG, tmp_path / 'missing.json')
    with pytest.raises(ProviderError, match='authorization_required'):
        asyncio.run(configured.quota())


def test_transport_errors_hide_urls_and_metadata_is_bounded(provider):
    def unavailable(request):
        raise httpx.ConnectError('private credential ' + UPLOAD, request=request)
    with pytest.raises(ProviderError, match='provider_unavailable') as raised:
        asyncio.run(provider(unavailable).quota())
    assert raised.value.__cause__ is None
    def huge(request):
        return httpx.Response(200, content=b' ' * (1024 * 1024 + 1))
    with pytest.raises(ProviderError, match='invalid_provider_response'):
        asyncio.run(provider(huge).quota())
