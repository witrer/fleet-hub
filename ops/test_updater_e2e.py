#!/usr/bin/env python3
"""Isolated Linux update/rollback test. Uses a local bare Git repository and HTTP servers."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

from fleet_update import Updater
from install_updater import bootstrap


HELPER = r'''
import http.server, json, os, pathlib, signal, subprocess, sys, time
root = pathlib.Path(sys.argv[2])
if sys.argv[1] == 'restart':
    pid_path = root / 'http.pid'
    if pid_path.exists():
        try: os.kill(int(pid_path.read_text()), signal.SIGTERM)
        except ProcessLookupError: pass
        time.sleep(.15)
    count = root / 'restarts'
    count.write_text(str(int(count.read_text()) + 1 if count.exists() else 1))
    with (root / 'http.log').open('ab') as output:
        proc = subprocess.Popen([sys.executable, __file__, 'serve', str(root)],
                                stdout=output, stderr=output, start_new_session=True)
    pid_path.write_text(str(proc.pid))
else:
    mode = (root / 'base' / 'current' / 'app' / 'mode').read_text()
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers()
            self.wfile.write(body)
        def do_GET(self): self.reply({'ok': mode == 'good'})
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            result = {'protocolVersion': '2025-11-25', 'capabilities': {}, 'serverInfo': {'name': 'e2e', 'version': '1'}}
            if payload['method'] == 'tools/list':
                result = {'tools': [{'name': name} for name in ['ssh_exec', 'ssh_spawn', 'fleet_status', 'fleet_enroll', 'fleet_revoke']]}
            self.reply({'jsonrpc': '2.0', 'id': payload['id'], 'result': result})
    class Server(http.server.HTTPServer): allow_reuse_address = True
    Server(('127.0.0.1', int((root / 'port').read_text())), Handler).serve_forever()
'''


def git(*args, cwd=None):
    return subprocess.check_output(['git', *map(str, args)], cwd=cwd, stderr=subprocess.PIPE, text=True).strip()


def main():
    root = Path(tempfile.mkdtemp(prefix='fleet-updater-e2e-'))
    checks = []
    try:
        bare, source = root / 'origin.git', root / 'source'
        git('init', '--bare', bare)
        git('init', '-b', 'main', source)
        git('config', 'user.email', 'e2e@localhost', cwd=source)
        git('config', 'user.name', 'Fleet isolated test', cwd=source)
        git('remote', 'add', 'origin', bare, cwd=source)
        for directory in ('app/static', 'app/downloads', 'wheels'):
            (source / directory).mkdir(parents=True, exist_ok=True)
        for name in ('app/app.py', 'app/mcp_entry.py'):
            (source / name).write_text('# valid test source\n')
        (source / 'app/static/index.html').write_text('test')
        (source / 'app/downloads/.gitkeep').touch()
        (source / 'wheels/.gitkeep').touch()
        real_wheels = Path(__file__).resolve().parent.parent / 'wheels'
        if not real_wheels.is_dir():
            real_wheels = Path('/opt/fleet/repo/wheels')
        wheels = list(real_wheels.glob('*.whl'))
        assert wheels, 'The existing Fleet wheel is required for this test'
        for wheel in wheels:
            shutil.copyfile(wheel, source / 'wheels' / wheel.name)
        def commit(mode, label, syntax=False):
            (source / 'app/mode').write_text(mode)
            (source / 'app/app.py').write_text('this is invalid (\n' if syntax else '# ' + label + '\n')
            git('add', '.', cwd=source)
            git('commit', '-m', label, cwd=source)
            git('push', '-u', 'origin', 'main', cwd=source)
            return git('rev-parse', 'HEAD', cwd=source)
        first = commit('good', 'first good release')
        base, data = root / 'base', root / 'data'
        (base / 'app/downloads').mkdir(parents=True)
        (base / 'app').chmod(0o777)
        (base / 'app/mode').write_text('good')
        payload = os.urandom(4096)
        (base / 'app/downloads/archive.bin').write_bytes(payload)
        data.mkdir()
        (data / 'runtime-marker').write_text('preserve this')
        git('clone', '-b', 'main', bare, base / 'repo')
        bootstrap(base)
        assert (base / 'app').is_symlink()
        assert (base / 'app').stat().st_mode & 0o777 == 0o755
        assert (base / 'app/downloads/archive.bin').read_bytes() == payload
        checks.append('bootstrap preserves app/downloads and fixes code permissions')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        (root / 'port').write_text(str(port))
        (root / 'helper.py').write_text(HELPER)
        cfg = {'base_dir': str(base), 'data_dir': str(data), 'repo_path': str(base / 'repo'),
               'python': sys.executable, 'health_url': f'http://127.0.0.1:{port}/health',
               'mcp_url': f'http://127.0.0.1:{port}/mcp', 'health_timeout': 2, 'health_interval': .1,
               'restart_command': [sys.executable, str(root / 'helper.py'), 'restart', str(root)],
               'lock_path': str(root / 'update.lock')}
        def run(): return Updater(cfg).run()
        def state(): return json.loads((data / 'update-status.json').read_text())
        def count(): return int((root / 'restarts').read_text())
        assert run() == 0 and state()['current_commit'] == first and count() == 1
        assert (base / 'app/downloads').is_symlink()
        assert list((base / 'current/vendor').glob('relay_shell-*.dist-info'))
        checks.append('wheel installs offline and healthy commit passes real hub/MCP HTTP checks')
        assert run() == 0 and count() == 1
        checks.append('unchanged SHA does not restart services')
        broken = commit('bad', 'health fails')
        assert run() == 1 and state()['status'] == 'rolled_back' and count() == 3
        assert state()['current_commit'] == first and (base / 'current').resolve().name == first
        assert run() == 0 and count() == 3 and state()['last_failed_commit'] == broken
        checks.append('bad health rolls back; failed SHA is not repeatedly redeployed')
        commit('good', 'syntax fails before activation', syntax=True)
        assert run() == 1 and state()['status'] == 'failed' and count() == 3
        assert (base / 'current').resolve().name == first
        assert run() == 0 and count() == 3
        checks.append('syntax errors are rejected before release activation')
        second = commit('good', 'second good release')
        assert run() == 0 and state()['current_commit'] == second and count() == 4
        assert sorted(p.name for p in (base / 'releases').iterdir()) == sorted([first, second])
        assert (data / 'runtime-marker').read_text() == 'preserve this'
        assert hashlib.sha256((base / 'app/downloads/archive.bin').read_bytes()).digest() == hashlib.sha256(payload).digest()
        checks.append('new commit recovers automatically; runtime data/downloads survive and old releases prune')
        interrupted = commit('bad', 'interrupted health check')
        cfg['health_timeout'] = 30
        config = root / 'config.json'
        config.write_text(json.dumps(cfg))
        with (root / 'interrupted.log').open('w') as output:
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('fleet_update.py')),
                                        '--config', str(config)], stdout=output, stderr=output)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if (base / 'current').resolve().name == interrupted and count() >= 5:
                    break
                if process.poll() is not None:
                    raise AssertionError('Updater exited before interruption test')
                time.sleep(.1)
            else:
                process.kill()
                raise AssertionError('Updater did not activate interruption test release')
            time.sleep(.3)
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=15) == 1
        assert state()['status'] == 'rolled_back' and count() == 6
        assert (base / 'current').resolve().name == second and Updater(cfg).healthy()
        checks.append('SIGTERM after activation restores the healthy previous release')
        print(json.dumps({'ok': True, 'checks': checks}, indent=2))
    except BaseException:
        print('Failed test artifacts: ' + str(root), file=sys.stderr)
        raise
    finally:
        pid_path = root / 'http.pid'
        if pid_path.exists():
            try:
                os.kill(int(pid_path.read_text()), signal.SIGTERM)
            except ProcessLookupError:
                pass
    shutil.rmtree(root)


if __name__ == '__main__':
    main()
