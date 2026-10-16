"""Actual Bash/curl launcher with synthetic uv and an isolated loopback endpoint."""
import hashlib
import http.server
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import threading

import pytest


@pytest.mark.skipif(platform.system() != 'Linux', reason='Bash runtime fixture targets Linux')
def test_upload_launcher_reuses_script_on_304_and_preserves_cache_on_failure(tmp_path):
    state = {'source': b'# fixture upload tool\n', 'fail': False}
    requests = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            assert self.path == '/transfer.py'
            etag = '"' + hashlib.sha256(state['source']).hexdigest() + '"'
            validator = self.headers.get('If-None-Match')
            status = 403 if state['fail'] else 304 if validator == etag else 200
            requests.append((status, validator))
            self.send_response(status)
            self.send_header('ETag', etag)
            self.end_headers()
            if status == 200:
                self.wfile.write(state['source'])
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        hub = 'http://127.0.0.1:' + str(server.server_port)
        client = tmp_path / 'client'
        (client / 'bin').mkdir(parents=True)
        target = {'x86_64': 'x86_64', 'aarch64': 'aarch64'}[platform.machine()] + '-unknown-linux-musl'
        uv = client / 'bin' / ('uv-0.13.1-' + target)
        # This fake cached runtime logs invocation and never executes a transfer.
        uv.write_text('#!/bin/sh\nprintf "ran\\n" >> "$FLEET_LAUNCH_LOG"\n')
        uv.chmod(0o700)
        Path(str(uv) + '.sha256').write_text(hashlib.sha256(uv.read_bytes()).hexdigest())
        wrapper = tmp_path / 'tools'
        wrapper.mkdir()
        curl = shutil.which('curl')
        assert curl is not None
        # Reject any unexpected network destination, including dependency mirrors.
        (wrapper / 'curl').write_text('#!/bin/bash\nfor arg in "$@"; do\n'
            'case "$arg" in http://127.0.0.1:*) ;; http:*|https:*) exit 96 ;; esac\ndone\n'
            'exec "$FLEET_TEST_CURL" "$@"\n')
        (wrapper / 'curl').chmod(0o700)
        source = tmp_path / 'dataset-fixture'
        source.write_text('tiny synthetic dataset')
        log = tmp_path / 'launches'
        env = {**os.environ, 'PATH': str(wrapper) + os.pathsep + os.environ['PATH'],
               'FLEET_CLIENT_HOME': str(client), 'FLEET_TOKEN': 'synthetic-test-token',
               'FLEET_LAUNCH_LOG': str(log), 'FLEET_TEST_CURL': curl}
        script = Path(__file__).with_name('static').joinpath('send.sh').read_text().replace('__HUB_URL__', hub)
        config = client / 'hubs' / hashlib.sha256(hub.encode()).hexdigest() / 'transfer.py'
        def launch():
            return subprocess.run(['bash', '-s', '--', '--node', 'node-fixture', '--source', str(source),
                                   '--destination', '/synthetic-target'], input=script,
                                  text=True, capture_output=True, env=env, timeout=10)
        first = launch()
        assert first.returncode == 0, first.stderr
        stamp = config.stat().st_mtime_ns
        assert config.read_bytes() == state['source']
        assert launch().returncode == 0
        assert config.stat().st_mtime_ns == stamp
        assert [row[0] for row in requests] == [200, 304]
        state['source'] = b'# changed fixture upload tool\n'
        assert launch().returncode == 0
        assert config.read_bytes() == state['source']
        state['fail'] = True
        failed = launch()
        assert failed.returncode != 0
        assert config.read_bytes() == state['source']
        assert log.read_text().splitlines() == ['ran', 'ran', 'ran']
        assert not list(client.glob('.prepare.*'))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
