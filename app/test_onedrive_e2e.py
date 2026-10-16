"""Three real endpoints: Fleet ASGI hub, upload client, and node receiver.

Microsoft metadata is synthetic. Every file byte crosses the independent
loopback storage HTTP endpoint; signed HTTPS fixture URLs are mapped only in
this test's transport, without changing production TLS settings.
"""
import asyncio
import contextlib
import http.server
import importlib.util
import json
from pathlib import Path
import re
import socket
import threading
import time
import urllib.parse
import urllib.request

import pytest
import uvicorn

from onedrive import ProviderError
from test_app import ADMIN, body, grant, hub


BLOCK = 320 * 1024
STORAGE_ORIGIN = 'https://storage.fixture.invalid'
CAPABILITY = 'synthetic-e2e-capability'


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / 'static' / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


uploader = load('fleet_upload_e2e_tested', 'transfer.py')
node_agent = load('fleet_node_e2e_tested', 'node.py')


class MetadataProvider:
    """Graph facade: accepts/returns metadata only, never streams file bodies."""
    enabled = True

    def __init__(self, storage):
        self.storage = storage
        self.calls = []
        self.deleted = []

    async def quota(self):
        self.calls.append('quota')
        return {'remaining': 10 * 1024**3, 'total': 10 * 1024**3}

    async def find_item(self, name):
        self.calls.append('find_item')
        if name not in self.storage['items']:
            raise ProviderError('not_found', 404)
        return dict(self.storage['items'][name])

    async def create_upload(self, name, size):
        self.calls.append('create_upload')
        self.storage['sessions'][name] = {'size': size, 'offset': 0}
        self.storage['bytes'][name] = b''
        return {'upload_url': STORAGE_ORIGIN + '/upload/' + name + '?cap=' + CAPABILITY,
                'expires_at': time.time() + 3600}

    async def upload_status(self, url):
        self.calls.append('upload_status')
        name = urllib.parse.urlsplit(url).path.rsplit('/', 1)[-1]
        session = self.storage['sessions'][name]
        return {'next_expected_ranges': [str(session['offset']) + '-'], 'expires_at': time.time() + 3600}

    async def create_empty(self, name):
        self.calls.append('create_empty')
        self.storage['bytes'][name] = b''
        return publish(self.storage, name, 0)

    async def item(self, item_id):
        self.calls.append('item')
        for item in self.storage['items'].values():
            if item['id'] == item_id:
                return dict(item)
        raise ProviderError('not_found', 404)

    async def permanent_delete(self, item_id, *, expected_name, expected_etag):
        self.calls.append('delete')
        item = await self.item(item_id)
        assert item['name'] == expected_name and item['etag'] == expected_etag
        self.storage['items'].pop(item['name'])
        self.storage['bytes'].pop(item['name'])
        self.storage['sessions'].pop(item['name'], None)
        self.deleted.append(item_id)

    async def cancel_upload(self, url):
        self.calls.append('cancel_upload')
        name = urllib.parse.urlsplit(url).path.rsplit('/', 1)[-1]
        self.storage['sessions'].pop(name, None)

    async def close(self):
        pass


def publish(storage, name, size):
    item = {'id': 'cloud-' + name, 'name': name, 'size': size, 'etag': '"fixture-version-1"',
            'download_url': STORAGE_ORIGIN + '/download/' + name + '?cap=' + CAPABILITY}
    storage['items'][name] = item
    return dict(item)


@pytest.fixture
def storage():
    state = {'items': {}, 'sessions': {}, 'bytes': {}, 'uploaded': 0, 'downloaded': 0,
             'puts': [], 'gets': [], 'lose_first_reply': False, 'lost_upload_reply': False}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def json(self, code, value):
            encoded = json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def name(self, prefix):
            assert self.headers.get('Authorization') is None
            parsed = urllib.parse.urlsplit(self.path)
            assert parsed.path.startswith(prefix)
            assert urllib.parse.parse_qs(parsed.query) == {'cap': [CAPABILITY]}
            return parsed.path[len(prefix):]

        def do_PUT(self):
            name = self.name('/upload/')
            session = state['sessions'][name]
            start, end, size = map(int, re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', self.headers['Content-Range']).groups())
            assert start == session['offset'] and size == session['size']
            data = self.rfile.read(int(self.headers['Content-Length']))
            assert len(data) == end - start + 1
            assert end == size - 1 or len(data) % BLOCK == 0
            state['puts'].append((name, start, end))
            state['bytes'][name] += data
            state['uploaded'] += len(data)
            session['offset'] = end + 1
            if state['lose_first_reply'] and not state['lost_upload_reply'] and end + 1 < size:
                # Graph may expose a zero-byte placeholder while the accepted
                # first fragment remains available in its upload session.
                publish(state, name, 0)
                state['lost_upload_reply'] = True
                self.close_connection = True
                return
            if end + 1 == size:
                self.json(201, publish(state, name, size))
            else:
                self.json(202, {'nextExpectedRanges': [str(end + 1) + '-']})

        def do_GET(self):
            name = self.name('/download/')
            data = state['bytes'][name]
            offset = 0
            if self.headers.get('Range'):
                offset = int(re.fullmatch(r'bytes=(\d+)-', self.headers['Range']).group(1))
            self.send_response(206 if offset else 200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', str(len(data) - offset))
            if offset:
                self.send_header('Content-Range', f'bytes {offset}-{len(data) - 1}/{len(data)}')
            self.end_headers()
            self.wfile.write(data[offset:])
            state['gets'].append((name, offset))
            state['downloaded'] += len(data) - offset

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    state['origin'] = f'http://127.0.0.1:{server.server_port}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


class HubTraffic:
    def __init__(self, app):
        self.app = app
        self.file_bytes_in = 0
        self.file_bytes_out = 0
        self.metadata_bytes_in = 0
        self.redirects = []
        self.request_bodies = []

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get('headers', []))
        response_headers = {}
        status = None
        async def tracked_receive():
            message = await receive()
            chunk = message.get('body', b'')
            if chunk:
                assert headers.get(b'content-type') == b'application/json'
                assert scope['method'] != 'PUT'
                self.request_bodies.append(chunk)
                if b'content-range' in headers or headers.get(b'content-type') == b'application/octet-stream':
                    self.file_bytes_in += len(chunk)
                else:
                    self.metadata_bytes_in += len(chunk)
            return message
        async def tracked_send(message):
            nonlocal status, response_headers
            if message['type'] == 'http.response.start':
                status = message['status']
                response_headers = dict(message['headers'])
            if message['type'] == 'http.response.body':
                chunk = message.get('body', b'')
                if scope.get('path', '').endswith('/content'):
                    self.file_bytes_out += len(chunk)
                    assert status == 302 and not chunk
                    assert response_headers.get(b'cache-control') == b'no-store'
                    self.redirects.append(response_headers[b'location'].decode())
                elif chunk:
                    assert response_headers.get(b'content-type', b'').startswith(b'application/json')
            await send(message)
        await self.app(scope, tracked_receive, tracked_send)


@contextlib.contextmanager
def serve_hub(app):
    traffic = HubTraffic(app)
    bound = socket.socket()
    bound.bind(('127.0.0.1', 0))
    bound.listen(128)
    port = bound.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(traffic, lifespan='off', log_level='error', access_log=False))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [bound]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started, 'Loopback ASGI hub did not start'
    try:
        yield f'http://127.0.0.1:{port}', traffic
    finally:
        server.should_exit = True
        thread.join(5)
        bound.close()
        assert not thread.is_alive()


@pytest.mark.parametrize('directory,lose_first_reply', [(True, False), (False, False), (True, True)],
                         ids=['directory-with-empty-and-three-chunks', 'single-file', 'resume-past-zero-byte-placeholder'])
def test_upload_hub_node_storage_contract(hub, storage, tmp_path, monkeypatch, directory, lose_first_reply):
    module, client, _ = hub
    storage['lose_first_reply'] = lose_first_reply
    registered = client.post('/api/enroll', headers=grant(client), json=body()).json()
    beat = client.post('/api/nodes/' + registered['id'] + '/heartbeat',
                       headers={'Authorization': 'Bearer ' + registered['token']}, json={'relay_protocol': 1})
    assert beat.status_code == 200
    provider = MetadataProvider(storage)
    module.relay_transfers.provider = provider
    monkeypatch.setenv('FLEET_CLIENT_HOME', str(tmp_path / 'client'))
    monkeypatch.setattr(uploader, 'RELAY_CHUNK_SIZE', BLOCK)
    def no_ssh(*args, **kwargs):
        raise AssertionError('OneDrive relay must not open SSH or P2P connections')
    monkeypatch.setattr(module.asyncssh, 'connect', no_ssh)
    if directory:
        source = tmp_path / 'source-dataset'
        source.mkdir()
        (source / 'nested').mkdir()
        (source / 'empty-directory').mkdir()
        (source / 'empty.bin').touch()
        content = (b'\x00synthetic-dataset-only\xff' * (BLOCK // 20 + 1))[:BLOCK]
        (source / 'nested' / 'dataset.bin').write_bytes(content * 2 + b'final-fragment')
        destination = tmp_path / 'received-dataset'
    else:
        source = tmp_path / 'source.bin'
        source.write_bytes(b'\x00single-file-synthetic-payload\xff')
        destination = tmp_path / 'received' / 'renamed.bin'
    files, directories, total = uploader.collect_files(source)
    expected = {relative: local.read_bytes() for local, relative, _ in files}
    if directory:
        assert directories == ['', 'empty-directory', 'nested']
    original_http = uploader.relay_http
    opener = urllib.request.build_opener(node_agent.RelayNoRedirect())
    with serve_hub(module.app) as (origin, traffic):
        def map_request(request):
            parsed = urllib.parse.urlsplit(request.full_url)
            if parsed.scheme == 'https' and parsed.netloc == 'storage.fixture.invalid':
                assert request.get_header('Authorization') is None
                return urllib.request.Request(storage['origin'] + parsed.path + '?' + parsed.query,
                                              data=request.data, headers=dict(request.header_items()), method=request.get_method())
            assert request.full_url.startswith(origin + '/'), 'Test attempted a non-loopback connection'
            return request
        def upload_request(request, service, timeout=45):
            return original_http(map_request(request), service, timeout)
        def open_url(request, timeout):
            return opener.open(map_request(request), timeout=timeout)
        monkeypatch.setattr(uploader, 'relay_http', upload_request)
        task = uploader.upload_onedrive(origin, 'synthetic-admin-token', registered['id'], source, files,
                                       directories, str(destination), False)
        assert task['status'] == 'ready'
        job_path = '/api/relay/transfers/' + task['id']
        ready = uploader.relay_api(origin, job_path, 'synthetic-admin-token')
        assert ready['status'] == 'ready' and ready['uploaded_bytes'] == total and ready['downloaded_bytes'] == 0
        cfg = {'id': registered['id'], 'token': registered['token'], 'hub': origin}
        receiver = node_agent.RelayReceiver(tmp_path / 'node-state', cfg, open_url=open_url)
        receiver._receive()
        assert receiver.last_error is None
        finished = uploader.relay_api(origin, job_path, 'synthetic-admin-token')
        assert finished['status'] == 'completed'
        assert finished['uploaded_bytes'] == finished['downloaded_bytes'] == total
        for relative, data in expected.items():
            target = destination / relative if directory else destination
            assert target.read_bytes() == data
        if directory:
            assert (destination / 'empty-directory').is_dir()
            assert (destination / 'nested').is_dir()
            large = next(entry for entry in storage['sessions'].values() if entry['size'])
            assert large['offset'] > BLOCK * 2
            assert len(storage['puts']) == 3
        else:
            assert len(storage['puts']) == 1
        if lose_first_reply:
            assert storage['lost_upload_reply']
            assert [start for _, start, _ in storage['puts']] == [0, BLOCK, BLOCK * 2]
            assert provider.calls.count('create_upload') == 1
            assert provider.calls.count('upload_status') == 1
            assert 'cancel_upload' not in provider.calls
        state = uploader.relay_api(origin, '/api/state', 'synthetic-admin-token')
        public = json.dumps(state)
        assert CAPABILITY not in public and STORAGE_ORIGIN not in public
        assert 'upload_url' not in public and 'download_url' not in public
        for marker in (tmp_path / 'client' / 'relay-transfers').glob('*.json'):
            assert CAPABILITY not in marker.read_text() and STORAGE_ORIGIN not in marker.read_text()
        for marker in tmp_path.rglob('.fleet-relay-*.json'):
            assert CAPABILITY not in marker.read_text() and STORAGE_ORIGIN not in marker.read_text()
        assert storage['uploaded'] == storage['downloaded'] == total
        assert traffic.file_bytes_in == traffic.file_bytes_out == 0
        assert traffic.metadata_bytes_in > 0 and traffic.redirects
        assert all(value.startswith(STORAGE_ORIGIN + '/download/') for value in traffic.redirects)
        # Source bytes never appeared in an otherwise-JSON hub request.
        assert all(b'\x00synthetic-dataset-only\xff' not in value for value in traffic.request_bodies)
    asyncio.run(module.relay_transfers.cleanup_once())
    assert len(provider.deleted) == len(files)
    assert not storage['items'] and not storage['bytes'] and not storage['sessions']
    assert module.relay_transfers.job(task['id'])['cleanup_pending'] == 0
