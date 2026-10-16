"""Synthetic loopback OneDrive upload sessions; no Microsoft account or real data."""
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import threading

import pytest


spec = importlib.util.spec_from_file_location('fleet_onedrive_transfer', Path(__file__).parent / 'static/transfer.py')
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)
TOKEN = 'synthetic-hub-token'
CAPABILITY = 'synthetic-upload-capability'
BLOCK = 320 * 1024


@pytest.fixture
def relay(tmp_path, monkeypatch):
    state = {'jobs': {}, 'keys': {}, 'objects': {}, 'events': [], 'behavior': None, 'creates': 0, 'puts': [], 'generation': 0}
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def reply(self, status, body):
            value = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(value)))
            self.send_header('Retry-After', '0')
            self.end_headers()
            self.wfile.write(value)

        def do_GET(self):
            assert self.headers.get('Authorization') == 'Bearer ' + TOKEN
            state['events'].append(('GET', self.path))
            task_id = self.path.rsplit('/', 1)[-1]
            self.reply(200, state['jobs'][task_id])

        def do_POST(self):
            assert self.headers.get('Authorization') == 'Bearer ' + TOKEN
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['events'].append(('POST', self.path))
            if self.path == '/api/relay/transfers':
                key = body['idempotency_key']
                if key not in state['keys']:
                    state['creates'] += 1
                    task_id = 'task-' + str(state['creates'])
                    state['keys'][key] = task_id
                    job = {**body, 'id': task_id, 'status': 'uploading', 'files': [
                        {**entry, 'id': 'file-' + str(index), 'status': 'pending'} for index, entry in enumerate(body['files'])]}
                    state['jobs'][task_id] = job
                    for entry in job['files']:
                        state['objects'][task_id, entry['id']] = {'bytes': b'', 'complete': False, 'entry': entry}
                self.reply(200, state['jobs'][state['keys'][key]])
                return
            parts = self.path.split('/')
            task_id, action = parts[4], parts[-1]
            job = state['jobs'][task_id]
            if action == 'ready':
                assert all(entry['status'] == 'uploaded' for entry in job['files'])
                job['status'] = 'ready'
                self.reply(200, job)
                return
            obj = state['objects'][task_id, parts[6]]
            if action == 'session':
                if obj['entry']['size'] == 0:
                    obj['complete'] = True
                if obj['complete']:
                    obj['entry']['status'] = 'uploaded'
                    self.reply(200, {'uploaded': True})
                else:
                    self.reply(200, {'upload_url': f'{state["hub"]}/upload/{task_id}/{parts[6]}/{state["generation"]}?cap={CAPABILITY}',
                                     'next_offset': len(obj['bytes']), 'expires_at': '2030-01-01T00:00:00Z'})
            elif action == 'complete':
                if obj['complete']:
                    obj['entry']['status'] = 'uploaded'
                    self.reply(200, {'uploaded': True})
                else:
                    self.reply(409, {'error': 'not_complete', 'secret': CAPABILITY})
            elif action == 'upload-progress':
                assert body['bytes_done'] <= len(obj['bytes'])
                self.reply(503, {'secret': CAPABILITY})  # Monitoring failure must not stop uploading.
            else:
                raise AssertionError(self.path)

        def do_PUT(self):
            assert self.headers.get('Authorization') is None
            assert CAPABILITY in self.path
            parts = self.path.split('?')[0].split('/')
            task_id, file_id = parts[2:4]
            obj = state['objects'][task_id, file_id]
            start, end, total = map(int, re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', self.headers['Content-Range']).groups())
            body = self.rfile.read(int(self.headers['Content-Length']))
            assert len(body) == end - start + 1
            assert total == obj['entry']['size']
            assert end == total - 1 or len(body) % BLOCK == 0
            state['events'].append(('PUT', start))
            state['puts'].append((task_id, file_id, start, end))
            behavior = state['behavior']
            if behavior and behavior(self, obj, start, end, total, body):
                return
            assert start == len(obj['bytes'])
            obj['bytes'] += body
            obj['complete'] = end + 1 == total
            self.reply(201 if obj['complete'] else 202,
                       {'id': 'fixture-item'} if obj['complete'] else {'nextExpectedRanges': [f'{len(obj["bytes"])}-']})

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    state['hub'] = f'http://127.0.0.1:{server.server_port}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('FLEET_CLIENT_HOME', str(tmp_path / 'client'))
    monkeypatch.setattr(transfer, 'RELAY_CHUNK_SIZE', BLOCK)
    monkeypatch.setattr(transfer.time, 'sleep', lambda _: None)
    state['cache'] = tmp_path / 'client' / 'relay-transfers'
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def run(relay, source):
    files, directories, _ = transfer.collect_files(source)
    return transfer.upload_onedrive(relay['hub'], TOKEN, 'node-fixture', source, files, directories, '/dataset', False)


def payload(tmp_path, size=BLOCK * 2 + 13):
    source = tmp_path / 'fixture.bin'
    source.write_bytes(bytes(range(256)) * (size // 256) + b'x' * (size % 256))
    return source


def test_directory_chunks_empty_file_and_safe_restart(relay, tmp_path, capsys):
    source = tmp_path / 'dataset'
    source.mkdir()
    (source / 'empty-directory').mkdir()
    (source / 'empty').touch()
    data = source / 'data.bin'
    data.write_bytes(b'z' * (BLOCK * 2 + 17))
    assert run(relay, source)['status'] == 'ready'
    job = relay['jobs']['task-1']
    assert job['directories'] == ['', 'empty-directory']
    assert relay['objects']['task-1', 'file-0']['bytes'] == data.read_bytes()
    before = list(relay['puts'])
    assert run(relay, source)['status'] == 'ready'
    assert relay['creates'] == 1 and relay['puts'] == before
    saved = next(relay['cache'].glob('*.json')).read_text()
    output = capsys.readouterr()
    for secret in (TOKEN, CAPABILITY, '/upload/'):
        assert secret not in saved + output.out + output.err
    assert 'delivery is pending' in output.out
    if os.name != 'nt':
        assert next(relay['cache'].glob('*.json')).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('kind', ['429', '503', '416', 'lost-intermediate'])
def test_fragment_errors_reconcile_server_offset(relay, tmp_path, kind):
    source = payload(tmp_path)
    used = []
    def behavior(handler, obj, start, end, total, body):
        if used:
            return False
        used.append(True)
        if kind in {'416', 'lost-intermediate'}:
            obj['bytes'] += body  # The first fragment reached storage before its reply was lost.
        if kind == 'lost-intermediate':
            handler.connection.shutdown(socket.SHUT_RDWR)
            handler.connection.close()
        else:
            handler.reply(int(kind), {'secret': CAPABILITY})
        return True
    relay['behavior'] = behavior
    assert run(relay, source)['status'] == 'ready'
    assert relay['objects']['task-1', 'file-0']['bytes'] == source.read_bytes()
    offsets = [row[2] for row in relay['puts']]
    assert offsets == ([0, BLOCK, BLOCK * 2] if kind in {'416', 'lost-intermediate'} else [0, 0, BLOCK, BLOCK * 2])


def test_expired_upload_url_restarts_from_reconciled_offset(relay, tmp_path):
    source = payload(tmp_path)
    expired = []
    def behavior(handler, obj, start, end, total, body):
        if start != BLOCK or expired:
            return False
        expired.append(True)
        obj['bytes'] = b''
        relay['generation'] += 1
        handler.reply(404, {'url': CAPABILITY})
        return True
    relay['behavior'] = behavior
    assert run(relay, source)['status'] == 'ready'
    assert [row[2] for row in relay['puts']] == [0, BLOCK, 0, BLOCK, BLOCK * 2]
    assert relay['objects']['task-1', 'file-0']['bytes'] == source.read_bytes()


def test_lost_final_response_probes_completion_without_reupload(relay, tmp_path):
    source = payload(tmp_path)
    def behavior(handler, obj, start, end, total, body):
        if end + 1 != total:
            return False
        obj['bytes'] += body
        obj['complete'] = True
        handler.connection.shutdown(socket.SHUT_RDWR)
        handler.connection.close()
        return True
    relay['behavior'] = behavior
    assert run(relay, source)['status'] == 'ready'
    final = relay['events'].index(('PUT', BLOCK * 2))
    assert relay['events'][final + 1][1].endswith('/complete')
    assert len([row for row in relay['events'] if row[0] == 'POST' and row[1].endswith('/session')]) == 1
    assert relay['objects']['task-1', 'file-0']['bytes'] == source.read_bytes()


def test_interrupted_process_state_resumes_without_repeating_first_fragment(relay, tmp_path, monkeypatch):
    source = payload(tmp_path)
    real_http = transfer.relay_http
    def interrupt(request, service, timeout=45):
        if service == 'OneDrive' and request.get_header('Content-range').startswith(f'bytes {BLOCK}-'):
            raise KeyboardInterrupt
        return real_http(request, service, timeout)
    with monkeypatch.context() as scoped:
        scoped.setattr(transfer, 'relay_http', interrupt)
        with pytest.raises(KeyboardInterrupt):
            run(relay, source)
    saved = json.loads(next(relay['cache'].glob('*.json')).read_text())
    assert saved['id'] == 'task-1'
    assert run(relay, source)['status'] == 'ready'
    assert relay['creates'] == 1
    assert [row[2] for row in relay['puts']] == [0, BLOCK, BLOCK * 2]


def test_actual_process_crash_resumes_from_persisted_task_and_server_offset(relay, tmp_path):
    source = payload(tmp_path)
    script = '''import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("fixture_transfer", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.RELAY_CHUNK_SIZE = 320 * 1024
original = module.relay_http
def request(req, service, timeout=45):
    response = original(req, service, timeout)
    if service == "OneDrive" and os.environ.get("FLEET_TEST_CRASH"):
        os._exit(33)
    return response
module.relay_http = request
sys.argv = [sys.argv[1]] + sys.argv[2:]
raise SystemExit(module.main())
'''
    command = [sys.executable, '-c', script, str(Path(__file__).parent / 'static/transfer.py'),
               '--hub', relay['hub'], '--node', 'node-fixture', '--source', str(source),
               '--destination', '/dataset', '--transport', 'onedrive']
    env = {**os.environ, 'FLEET_TOKEN': TOKEN, 'FLEET_TEST_CRASH': '1'}
    result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=10)
    assert result.returncode == 33, result.stderr
    assert relay['objects']['task-1', 'file-0']['bytes'] == source.read_bytes()[:BLOCK]
    env.pop('FLEET_TEST_CRASH')
    result = subprocess.run(command, capture_output=True, text=True, env=env, timeout=10)
    assert result.returncode == 0, result.stderr
    assert relay['creates'] == 1
    assert [row[2] for row in relay['puts']] == [0, BLOCK, BLOCK * 2]
    assert relay['objects']['task-1', 'file-0']['bytes'] == source.read_bytes()


def test_progress_failure_has_small_budget_and_does_not_retry_or_block_upload(relay, tmp_path, monkeypatch):
    source = payload(tmp_path)
    clock = [0]
    def advance():
        clock[0] += 6
        return clock[0]
    monkeypatch.setattr(transfer.time, 'monotonic', advance)
    original = transfer.relay_http
    budgets = []
    def request(req, service, timeout=45):
        if req.full_url.endswith('/upload-progress'):
            budgets.append(timeout)
        return original(req, service, timeout)
    monkeypatch.setattr(transfer, 'relay_http', request)
    assert run(relay, source)['status'] == 'ready'
    assert budgets == [4, 4]
    assert len(relay['puts']) == 3


def test_lost_task_creation_reply_reuses_saved_idempotency_key(relay, tmp_path, monkeypatch):
    source = payload(tmp_path, 13)
    original = transfer.relay_http
    lost = []
    def request(req, service, timeout=45):
        response = original(req, service, timeout)
        if req.full_url.endswith('/api/relay/transfers') and not lost:
            lost.append(True)
            raise transfer.RelayUncertainError('Synthetic lost reply')
        return response
    monkeypatch.setattr(transfer, 'relay_http', request)
    assert run(relay, source)['status'] == 'ready'
    assert relay['creates'] == 1
    assert len([event for event in relay['events'] if event == ('POST', '/api/relay/transfers')]) == 2


def test_persistent_storage_failure_is_bounded_and_keeps_resume_state(relay, tmp_path, capsys):
    source = payload(tmp_path, 13)
    relay['behavior'] = lambda handler, *args: (handler.reply(503, {'secret': CAPABILITY}) or True)
    with pytest.raises(RuntimeError, match='retries exhausted'):
        run(relay, source)
    assert len(relay['puts']) == transfer.RELAY_ATTEMPTS
    assert json.loads(next(relay['cache'].glob('*.json')).read_text())['id'] == 'task-1'
    assert not any(event[1].endswith('/ready') for event in relay['events'] if event[0] == 'POST')
    output = capsys.readouterr()
    assert CAPABILITY not in output.out + output.err


def test_changed_source_refuses_active_resume_and_allows_terminal_new_task(relay, tmp_path, monkeypatch):
    source = payload(tmp_path)
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    with monkeypatch.context() as scoped:
        scoped.setattr(transfer, 'upload_relay_file', interrupt)
        with pytest.raises(KeyboardInterrupt):
            run(relay, source)
    source.write_bytes(b'changed source')
    with pytest.raises(RuntimeError, match='Source changed'):
        run(relay, source)
    assert relay['creates'] == 1 and not relay['puts']
    relay['jobs']['task-1']['status'] = 'expired'
    assert run(relay, source)['status'] == 'ready'
    assert relay['creates'] == 2


def test_same_size_source_change_during_upload_never_releases_task(relay, tmp_path):
    source = payload(tmp_path)
    original = source.stat()
    changed = []
    def behavior(handler, obj, start, end, total, body):
        if not changed:
            changed.append(True)
            os.utime(source, ns=(original.st_atime_ns, original.st_mtime_ns + 1000000000))
        return False
    relay['behavior'] = behavior
    with pytest.raises(RuntimeError, match='Source changed'):
        run(relay, source)
    assert relay['jobs']['task-1']['status'] == 'uploading'
    assert not any(event[1].endswith('/ready') for event in relay['events'] if event[0] == 'POST')


def test_errors_do_not_print_signed_urls_or_response_body(relay, tmp_path, capsys):
    source = payload(tmp_path, 13)
    relay['behavior'] = lambda handler, *args: (handler.reply(400, {'secret': CAPABILITY, 'token': TOKEN}) or True)
    with pytest.raises(transfer.RelayHTTPError, match='OneDrive returned HTTP 400') as caught:
        run(relay, source)
    output = capsys.readouterr()
    assert CAPABILITY not in str(caught.value) + output.out + output.err
    assert TOKEN not in str(caught.value) + output.out + output.err


def test_hub_redirect_never_sends_bearer_to_redirect_target(relay, monkeypatch):
    destinations = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            destinations.append(self.path)
            self.send_response(302)
            self.send_header('Location', relay['hub'] + '/would-leak-bearer')
            self.end_headers()
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(transfer.RelayHTTPError, match='HTTP 302'):
            transfer.relay_api(f'http://127.0.0.1:{server.server_port}', '/metadata', TOKEN)
        assert destinations == ['/metadata']
        assert relay['events'] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_parallel_local_upload_is_rejected(relay, tmp_path):
    path = relay['cache'] / 'fixture.json'
    path.parent.mkdir(parents=True)
    with transfer.relay_state_lock(path):
        with pytest.raises(RuntimeError, match='Another uploader'):
            with transfer.relay_state_lock(path):
                pytest.fail('Acquired a duplicate lock')


@pytest.mark.skipif(platform.system() != 'Linux', reason='Bash runtime fixture targets Linux')
def test_bash_launcher_passes_transport_with_existing_cached_runtime(relay, tmp_path):
    client = tmp_path / 'client'
    (client / 'bin').mkdir(parents=True)
    target = {'x86_64': 'x86_64', 'aarch64': 'aarch64'}[platform.machine()] + '-unknown-linux-musl'
    uv = client / 'bin' / ('uv-0.13.1-' + target)
    uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$FLEET_TEST_ARGS"\n')
    uv.chmod(0o700)
    Path(str(uv) + '.sha256').write_text(hashlib.sha256(uv.read_bytes()).hexdigest())
    tools = tmp_path / 'tools'
    tools.mkdir()
    # Return a small synthetic cached script without contacting any network.
    (tools / 'curl').write_text('#!/bin/bash\nwhile (($#)); do\n'
        'if [[ "$1" == -o ]]; then printf "# test script\\n" > "$2"; shift 2; else shift; fi\ndone\nprintf 200\n')
    (tools / 'curl').chmod(0o700)
    source = payload(tmp_path, 5)
    arguments = tmp_path / 'args'
    env = {**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH'],
           'FLEET_CLIENT_HOME': str(client), 'FLEET_TOKEN': TOKEN, 'FLEET_TEST_ARGS': str(arguments)}
    script = Path(__file__).with_name('static').joinpath('send.sh').read_text().replace('__HUB_URL__', relay['hub'])
    command = ['bash', '-s', '--', '--node', 'node', '--source', str(source), '--destination', '/data']
    for flags, expected in [([], 'p2p'), (['--transport', 'onedrive'], 'onedrive')]:
        result = subprocess.run(command + flags, input=script, text=True, capture_output=True, env=env, timeout=10)
        assert result.returncode == 0, result.stderr
        values = arguments.read_text().splitlines()
        assert values[values.index('--transport') + 1] == expected
    result = subprocess.run(command + ['--transport', 'unknown'], input=script, text=True, capture_output=True, env=env, timeout=10)
    assert result.returncode != 0
