"""Authorization tests use synthetic responses only; no account is contacted."""
import importlib.util
import io
import json
from pathlib import Path
import stat
import urllib.error
import urllib.parse
import urllib.request

import pytest


spec = importlib.util.spec_from_file_location('authorize_onedrive', Path(__file__).with_name('authorize_onedrive.py'))
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)
CLIENT_ID = '00000000-1111-2222-3333-444444444444'
DEVICE = {'device_code': 'synthetic-device-secret', 'user_code': 'TEST-CODE',
          'verification_uri': 'https://microsoft.com/devicelogin', 'expires_in': 900, 'interval': 2}
TOKENS = {'access_token': 'synthetic-access-secret', 'refresh_token': 'synthetic-refresh-secret'}
DRIVE = {'id': 'drive-id', 'driveType': 'personal', 'quota': {'remaining': 20_000_000_000}}
FOLDER = {'id': 'folder-id', 'folder': {}}


class Clock:
    def __init__(self):
        self.now = 0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.waits.append(delay)
        self.now += delay


class FakeHTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError('Unexpected HTTP request: ' + method)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def invoke(root, http, *, timeout=900, folder='FleetHubTransfers'):
    clock, output = Clock(), []
    result = setup.main(['--root', str(root), '--client-id', CLIENT_ID, '--timeout', str(timeout),
                         '--folder', folder], http=http, clock=clock, sleep=clock.sleep, output=output.append)
    return result, clock, '\n'.join(output)


def test_authorization_and_existing_folder_preserve_config_and_hide_tokens(tmp_path):
    config = tmp_path / 'config.json'
    original = '{"existing": "synthetic-config"}\n'
    config.write_text(original)
    http = FakeHTTP((200, DEVICE), (400, {'error': 'authorization_pending'}), (200, TOKENS),
                    (200, DRIVE), (200, FOLDER))
    result, clock, output = invoke(tmp_path, http)
    assert result == 0 and clock.waits == [2, 2] and not http.responses
    assert config.read_text() == original
    token_file = tmp_path / 'onedrive-token.json'
    assert json.loads(token_file.read_text()) == {'refresh_token': TOKENS['refresh_token']}
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert not list(tmp_path.glob('.onedrive-token-*'))
    assert 'TEST-CODE' in output and '20000000000 remaining bytes' in output
    assert all(secret not in output for secret in (*TOKENS.values(), DEVICE['device_code']))
    assert '"tenant": "consumers"' in output and '"drive_id": "drive-id"' in output
    first = http.requests[0]
    assert first[1] == setup.AUTH + '/devicecode'
    assert first[2]['form'] == {'client_id': CLIENT_ID, 'scope': 'offline_access Files.ReadWrite'}
    assert all(call[2]['token'] == TOKENS['access_token'] for call in http.requests[-2:])
    assert all('/consumers/' in call[1] for call in http.requests[:3])


def test_folder_created_once_and_encoded_as_single_component(tmp_path):
    http = FakeHTTP((200, DEVICE), (200, TOKENS), (200, DRIVE), (404, {}), (201, FOLDER))
    result, _, _ = invoke(tmp_path, http, folder='Fleet 测试 #1')
    assert result == 0
    assert '/root:/Fleet%20%E6%B5%8B%E8%AF%95%20%231?' in http.requests[-2][1]
    assert http.requests[-1][0] == 'POST'
    assert http.requests[-1][2]['body'] == {
        'name': 'Fleet 测试 #1', 'folder': {}, '@microsoft.graph.conflictBehavior': 'fail'}


def test_concurrent_folder_create_reuses_exact_folder(tmp_path):
    http = FakeHTTP((200, DEVICE), (200, TOKENS), (200, DRIVE), (404, {}), (409, {}), (200, FOLDER))
    result, _, _ = invoke(tmp_path, http)
    assert result == 0
    assert http.requests[-1][1] == http.requests[-3][1]
    assert http.requests[-1][0] == 'GET'


def test_slow_down_changes_following_poll_interval():
    clock, output = Clock(), []
    http = FakeHTTP((200, DEVICE), (400, {'error': 'authorization_pending'}),
                    (400, {'error': 'slow_down'}), (200, TOKENS))
    assert setup.authorize(CLIENT_ID, http=http, clock=clock, sleep=clock.sleep, output=output.append) == TOKENS
    assert clock.waits == [2, 2, 7]


@pytest.mark.parametrize('response', [setup.NetworkError('synthetic network failure'), (503, {}), (429, {})])
def test_temporary_token_request_failures_retry_with_backoff(response):
    clock = Clock()
    http = FakeHTTP((200, DEVICE), response, (200, TOKENS))
    assert setup.authorize(CLIENT_ID, http=http, clock=clock, sleep=clock.sleep, output=lambda _: None) == TOKENS
    assert clock.waits == [2, 7]


@pytest.mark.parametrize('error', ['authorization_declined', 'access_denied', 'expired_token',
                                  'bad_verification_code', 'invalid_client'])
def test_terminal_authorization_errors_preserve_previous_token(tmp_path, error):
    token_file = tmp_path / 'onedrive-token.json'
    token_file.write_text('old-token-content')
    http = FakeHTTP((200, DEVICE), (400, {'error': error, 'error_description': 'synthetic-secret-must-stay-hidden'}))
    result, _, output = invoke(tmp_path, http)
    assert result == 1 and len(http.requests) == 2
    assert token_file.read_text() == 'old-token-content'
    assert 'synthetic-secret-must-stay-hidden' not in output


@pytest.mark.parametrize('local_timeout,server_timeout', [(3, 900), (900, 3)])
def test_polling_uses_earlier_deadline(tmp_path, local_timeout, server_timeout):
    http = FakeHTTP((200, dict(DEVICE, expires_in=server_timeout, interval=5)))
    result, clock, output = invoke(tmp_path, http, timeout=local_timeout)
    assert result == 1 and clock.now == 3 and len(http.requests) == 1
    assert 'timed out' in output and not (tmp_path / 'onedrive-token.json').exists()


@pytest.mark.parametrize('bad_tokens', [{'access_token': 'synthetic'}, {'refresh_token': 'synthetic'},
                                       {'access_token': 'synthetic', 'refresh_token': ''}])
def test_missing_tokens_never_saved(tmp_path, bad_tokens):
    result, _, _ = invoke(tmp_path, FakeHTTP((200, DEVICE), (200, bad_tokens)))
    assert result == 1 and not (tmp_path / 'onedrive-token.json').exists()


@pytest.mark.parametrize('drive_response,folder_response', [
    ((403, {}), None), ((200, dict(DRIVE, driveType='business')), None),
    ((200, DRIVE), (200, {'id': 'not-a-folder', 'file': {}})),
    ((200, DRIVE), (403, {'error': {'message': 'synthetic-secret-must-stay-hidden'}})),
])
def test_graph_failures_do_not_replace_existing_authorization(tmp_path, drive_response, folder_response):
    token_file = tmp_path / 'onedrive-token.json'
    token_file.write_text('previous-authorization')
    responses = [(200, DEVICE), (200, TOKENS), drive_response]
    if folder_response:
        responses.append(folder_response)
    result, _, output = invoke(tmp_path, FakeHTTP(*responses))
    assert result == 1 and token_file.read_text() == 'previous-authorization'
    assert 'synthetic-secret-must-stay-hidden' not in output


def test_atomic_replace_failure_keeps_previous_token_and_removes_temporary(tmp_path, monkeypatch):
    token_file = tmp_path / 'onedrive-token.json'
    token_file.write_text('old')
    def failed_replace(*_):
        raise OSError('synthetic disk failure')
    monkeypatch.setattr(setup.os, 'replace', failed_replace)
    result, _, output = invoke(tmp_path, FakeHTTP((200, DEVICE), (200, TOKENS), (200, DRIVE), (200, FOLDER)))
    assert result == 1 and token_file.read_text() == 'old'
    assert not list(tmp_path.glob('.onedrive-token-*'))
    assert TOKENS['refresh_token'] not in output


def test_replace_tightens_old_file_permissions(tmp_path):
    token_file = tmp_path / 'onedrive-token.json'
    token_file.write_text('old')
    token_file.chmod(0o644)
    setup.atomic_token(tmp_path, 'synthetic-new-refresh')
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


@pytest.mark.parametrize('bad_folder', ['../escape', 'folder/sub', '.', '..', 'trailing.', ' invalid', 'a\nb', 'x\\y'])
def test_invalid_folder_rejected_before_authorization(tmp_path, bad_folder):
    http = FakeHTTP()
    with pytest.raises(SystemExit):
        invoke(tmp_path, http, folder=bad_folder)
    assert http.requests == []


class FakeResponse(io.BytesIO):
    def __init__(self, data, code=200):
        super().__init__(data)
        self.code = code


class FakeOpener:
    def __init__(self, response):
        self.response, self.calls = response, []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_http_encodes_forms_and_passes_only_requested_authorization():
    opener = FakeOpener(FakeResponse(b'{"value": 1}'))
    assert setup.HTTPJSON(opener).request('POST', setup.AUTH + '/token',
                                         form={'device_code': 'synthetic+code', 'client_id': CLIENT_ID}) == (200, {'value': 1})
    request = opener.calls[0][0]
    assert urllib.parse.parse_qs(request.data.decode())['device_code'] == ['synthetic+code']
    assert not request.has_header('Authorization')
    assert request.get_header('Content-type') == 'application/x-www-form-urlencoded'


@pytest.mark.parametrize('raw', [b'not-json synthetic-secret', b'[]', b'x' * (setup.MAX_RESPONSE + 1)])
def test_http_rejects_invalid_or_oversized_response_without_body_leak(raw):
    with pytest.raises(setup.AuthorizationError) as error:
        setup.HTTPJSON(FakeOpener(FakeResponse(raw))).request('GET', setup.GRAPH + '/me/drive')
    assert 'synthetic-secret' not in str(error.value)


def test_http_reads_oauth_http_error_as_json_and_disables_redirects():
    error = urllib.error.HTTPError(setup.AUTH + '/token', 400, 'Bad Request', {},
                                  io.BytesIO(b'{"error": "authorization_pending"}'))
    assert setup.HTTPJSON(FakeOpener(error)).request('POST', setup.AUTH + '/token') == (
        400, {'error': 'authorization_pending'})
    assert any(isinstance(handler, setup.NoRedirect) for handler in setup.HTTPJSON().opener.handlers)
    request = urllib.request.Request(setup.GRAPH + '/me/drive', headers={'Authorization': 'Bearer synthetic'})
    assert setup.NoRedirect().redirect_request(request, None, 302, '', {}, 'https://other.test') is None
