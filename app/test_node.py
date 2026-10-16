"""Linux node lifecycle checks; all services and files stay in a temporary directory."""
import hashlib
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SOURCE = Path(__file__).parent / 'static/node.py'
GUARD_SOURCE = SOURCE.with_name('node-supervisor.py').read_bytes()
spec = importlib.util.spec_from_file_location('fleet_node_tested', SOURCE)
node = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node)


def active(pid):
    try:
        return Path('/proc', str(pid), 'stat').read_text().split()[2] != 'Z'
    except OSError:
        return False


def wait_for(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('Timed out waiting for node lifecycle condition')


class NodeTests(unittest.TestCase):
    @staticmethod
    def release_archive(name='frp_test/frpc', payload=b'fake executable', kind=tarfile.REGTYPE):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w:gz') as archive:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.size = len(payload) if kind == tarfile.REGTYPE else 0
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                member.linkname = '/etc/passwd'
            archive.addfile(member, io.BytesIO(payload) if member.size else None)
        return output.getvalue()

    def test_frp_install_streams_download_and_atomically_replaces_executable(self):
        payload = b'fixture executable\n' * 150000
        release = self.release_archive(payload=payload)

        class BoundedResponse(io.BytesIO):
            def read(self, size=-1):
                if not 0 < size <= 1024 * 1024:
                    raise AssertionError('Downloads must use bounded reads')
                return super().read(size)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'frpc'
            target.write_bytes(b'previous executable')
            with patch.object(node.urllib.request, 'urlopen', return_value=BoundedResponse(release)):
                node.install_frp(root, {'download_url': 'https://fixture.invalid/frp.tar.gz',
                                        'sha256': hashlib.sha256(release).hexdigest()})
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            self.assertEqual(list(root.iterdir()), [target])

    def test_frp_invalid_download_or_archive_preserves_previous_executable(self):
        releases = [
            ('wrong checksum', self.release_archive(), '0' * 64),
            ('invalid archive', b'not a gzip archive', None),
            ('missing frpc', self.release_archive(name='frp_test/frps'), None),
            ('absolute path', self.release_archive(name='/tmp/frpc'), None),
            ('traversal', self.release_archive(name='../frpc'), None),
            ('symlink', self.release_archive(kind=tarfile.SYMTYPE), None),
            ('hardlink', self.release_archive(kind=tarfile.LNKTYPE), None),
        ]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'frpc'
            target.write_bytes(b'previous executable')
            for name, release, checksum in releases:
                with self.subTest(name=name), patch.object(node.urllib.request, 'urlopen', return_value=io.BytesIO(release)):
                    with self.assertRaises((SystemExit, ValueError, tarfile.TarError)):
                        node.install_frp(root, {'download_url': 'https://fixture.invalid/frp.tar.gz',
                                                'sha256': checksum or hashlib.sha256(release).hexdigest()})
                self.assertEqual(target.read_bytes(), b'previous executable')
                self.assertEqual(list(root.iterdir()), [target])

    def test_frp_interrupted_download_or_disk_write_preserves_previous_executable(self):
        release = self.release_archive()
        metadata = {'download_url': 'https://fixture.invalid/frp.tar.gz',
                    'sha256': hashlib.sha256(release).hexdigest()}

        class InterruptedResponse(io.BytesIO):
            def read(self, size=-1):
                if self.tell():
                    raise OSError('download interrupted')
                return super().read(8)

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            target = root / 'frpc'
            target.write_bytes(b'previous executable')
            with patch.object(node.urllib.request, 'urlopen', return_value=InterruptedResponse(release)):
                with self.assertRaises(OSError):
                    node.install_frp(root, metadata)
            self.assertEqual(target.read_bytes(), b'previous executable')
            self.assertEqual(list(root.iterdir()), [target])

            def full_disk(source, output, **_):
                output.write(b'partial executable')
                raise OSError('disk full')

            with patch.object(node.urllib.request, 'urlopen', return_value=io.BytesIO(release)), \
                    patch.object(node.shutil, 'copyfileobj', side_effect=full_disk):
                with self.assertRaises(OSError):
                    node.install_frp(root, metadata)
            self.assertEqual(target.read_bytes(), b'previous executable')
            self.assertEqual(list(root.iterdir()), [target])

    def test_child_bootstrap_keeps_pid_and_refuses_a_missing_parent(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'child.pid'
            command = [sys.executable, '-c', 'import os,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))', str(target)]
            child = node.spawn_child(command)
            try:
                self.assertEqual(child.wait(timeout=5), 0)
                self.assertEqual(int(target.read_text()), child.pid, 'Bootstrap must exec without a lasting wrapper process')
            finally:
                node.terminate(child)
            target.unlink()
            with patch.object(node.os, 'getpid', return_value=-1):
                child = node.spawn_child(command)
            try:
                self.assertEqual(child.wait(timeout=5), 1)
                self.assertFalse(target.exists(), 'A child orphaned during bootstrap must not start FRP')
            finally:
                node.terminate(child)

    def test_one_command_installer_preserves_token_arguments(self):
        # Exercise Bash and the real node CLI, replacing only installation side effects.
        source = SOURCE.read_text().replace(
            "if __name__ == '__main__':",
            "def install(args):\n    print(json.dumps(vars(args)))\n\nif __name__ == '__main__':",
        ).encode()

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def do_GET(self):
                payload = source if self.path == '/node.py' else json.dumps({
                    'sha256': hashlib.sha256(source).hexdigest(),
                }).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(payload)

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                hub = 'http://127.0.0.1:' + str(server.server_port)
                env = {**os.environ, 'FLEET_HUB': hub, 'FLEET_NODE_DIR': folder,
                       'FLEET_PYTHON': sys.executable}
                for token in ('ordinary_test_token', '-synthetic_test_token', '--help', ''):
                    with self.subTest(token=token):
                        result = subprocess.run(
                            ['bash', '-s', '--', token],
                            input=SOURCE.with_name('install.sh').read_bytes(),
                            env=env, capture_output=True, timeout=20,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr.decode())
                        self.assertEqual(json.loads(result.stdout), {
                            'action': 'install', 'hub': hub, 'token': token,
                        })
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_repair_legacy_registration_without_consuming_token(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = {'hub': 'https://hub.example', 'id': 'node-123', 'token': 'keep-node-token', 'name': 'legacy'}
            (root / 'config.json').write_text(json.dumps(cfg))
            (root / 'ca.pem').write_text('ca')
            (root / 'frpc.toml').write_text('localPort = 2222\n')
            (root / 'frpc').write_text('binary')
            with patch.object(node, 'ensure_ssh') as ssh, patch.object(node, 'request') as request, \
                    patch.object(node, 'prepare_supervisor', return_value=GUARD_SOURCE), \
                    patch.object(node, 'start_service', return_value='test') as service:
                node.install_locked(SimpleNamespace(hub=cfg['hub'], token='unused-enrollment-token'), root)
            ssh.assert_called_once_with(2222)
            request.assert_called_once_with(cfg['hub'], '/api/agent-release', cfg['token'])
            service.assert_called_once_with(root)
            self.assertEqual(json.loads((root / 'config.json').read_text()), cfg)

    def test_update_checks_hash_and_preserves_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'node.py').write_bytes(b'# old\n')
            (root / 'config.json').write_bytes(b'credentials stay unchanged')
            source = b'# new valid release\n'
            release = {'sha256': hashlib.sha256(source).hexdigest(), 'url': 'https://hub.example/node.py', 'version': 'new'}
            cfg = {'hub': 'https://hub.example', 'token': 'token'}
            with patch.object(node, 'request', return_value=release), \
                    patch.object(node.urllib.request, 'urlopen', return_value=io.BytesIO(b'corrupt')):
                with self.assertRaises(ValueError):
                    node.check_update(root, cfg)
            self.assertEqual((root / 'node.py').read_bytes(), b'# old\n')
            with patch.object(node, 'prepare_supervisor', return_value=GUARD_SOURCE), \
                    patch.object(node, 'request', return_value=release), \
                    patch.object(node.urllib.request, 'urlopen', return_value=io.BytesIO(source)):
                self.assertTrue(node.check_update(root, cfg))
                self.assertFalse(node.check_update(root, cfg))
            self.assertEqual((root / 'config.json').read_bytes(), b'credentials stay unchanged')

    def test_blocked_update_worker_keeps_heartbeats_and_frp_supervision_running(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = {'hub': 'https://hub.example', 'id': 'fixture', 'token': 'synthetic'}
            (root / 'config.json').write_text(json.dumps(cfg))
            (root / 'node.py').write_bytes(b'# running release\n')
            updater = node.AgentUpdater(root, cfg)
            updater.next_check = 0
            entered, finish = threading.Event(), threading.Event()

            def blocked(*_):
                entered.set()
                finish.wait(2)
                return None

            heartbeat = Mock()
            child = SimpleNamespace(poll=lambda: 1)

            def next_heartbeat(_):
                self.assertTrue(entered.wait(1))
                if heartbeat.call_count >= 3:
                    raise SystemExit(0)

            try:
                with patch.object(node, 'prepare_update', side_effect=blocked) as prepare, \
                        patch.object(node, 'AgentUpdater', return_value=updater), \
                        patch.object(node, 'GPUCollector', return_value=SimpleNamespace(snapshot=lambda: {})), \
                        patch.object(node, 'cpu', return_value=(0, 0)), \
                        patch.object(node, 'metrics', return_value=((1, 0), {})), \
                        patch.object(node.HeartbeatLatency, 'send', heartbeat), \
                        patch.object(node, 'spawn_child', return_value=child) as spawn, \
                        patch.object(node, 'terminate') as terminate, \
                        patch.object(node.signal, 'signal'), \
                        patch.object(node.time, 'sleep', side_effect=next_heartbeat):
                    with self.assertRaises(SystemExit):
                        node.run(SimpleNamespace(root=root))
                    self.assertEqual(heartbeat.call_count, 3)
                    self.assertEqual(spawn.call_count, 3, 'FRP restarts must not wait for update HTTP')
                    prepare.assert_called_once_with(root, cfg)
                    terminate.assert_called_once_with(child)
                    self.assertTrue(updater.worker.is_alive())
                    self.assertFalse((root / 'run.pid').exists())
            finally:
                finish.set()
                if updater.worker is not None:
                    updater.worker.join(2)

    def test_blocked_resource_collection_keeps_first_heartbeat_and_frp_supervision_running(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cfg = {'hub': 'https://hub.example', 'id': 'fixture', 'token': 'synthetic'}
            (root / 'config.json').write_text(json.dumps(cfg))
            (root / 'node.py').write_bytes(b'# running release\n')
            collector = node.MetricsCollector()
            entered, finish = threading.Event(), threading.Event()
            payloads = []

            def blocked(_):
                entered.set()
                finish.wait(2)
                return (1, 0), {'cpu_percent': None}

            def heartbeat(_, payload):
                payloads.append(payload)

            def next_heartbeat(_):
                self.assertTrue(entered.wait(1))
                if len(payloads) >= 3:
                    raise SystemExit(0)

            child = SimpleNamespace(poll=lambda: 1)
            try:
                started = time.monotonic()
                with patch.object(node, 'metrics', side_effect=blocked) as collect, \
                        patch.object(node, 'MetricsCollector', return_value=collector), \
                        patch.object(node, 'GPUCollector', return_value=SimpleNamespace(snapshot=lambda: {})), \
                        patch.object(node.HeartbeatLatency, 'send', side_effect=heartbeat), \
                        patch.object(node, 'spawn_child', return_value=child) as spawn, \
                        patch.object(node, 'terminate') as terminate, \
                        patch.object(node.signal, 'signal'), \
                        patch.object(node.time, 'sleep', side_effect=next_heartbeat):
                    with self.assertRaises(SystemExit):
                        node.run(SimpleNamespace(root=root))
                    self.assertLess(time.monotonic() - started, .5)
                    self.assertEqual(len(payloads), 3)
                    self.assertEqual(spawn.call_count, 3)
                    collect.assert_called_once_with(None)
                    self.assertTrue(all(item['metrics_status'] == 'collecting' for item in payloads))
                    self.assertTrue(all(item['metrics_sampled_at'] is None for item in payloads))
                    self.assertTrue(all('cpu_percent' not in item for item in payloads))
                    terminate.assert_called_once_with(child)
                    self.assertTrue(collector.stopped.is_set())
                    self.assertTrue(collector.worker.is_alive(), 'shutdown must not wait on a blocked filesystem')
                    self.assertFalse((root / 'run.pid').exists())
            finally:
                finish.set()
                collector.worker.join(timeout=2)

    def test_update_worker_error_recovers_and_installs_only_on_main_poll(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            node.install_guard(root, b'# current\n', GUARD_SOURCE)
            current = root / 'node-agent.py'
            release = {'source': b'# verified replacement\n', 'version': 'test'}
            updater = node.AgentUpdater(root, {})
            with patch.object(node, 'prepare_update', side_effect=[TimeoutError('release timeout'), release]) as prepare, \
                    patch('builtins.print') as output:
                updater.next_check = 0
                self.assertFalse(updater.poll())
                updater.worker.join(2)
                self.assertFalse(updater.poll())
                self.assertIn('release timeout', output.call_args.args[0])
                self.assertEqual(current.read_bytes(), b'# current\n')
                self.assertIsNone(updater.worker)
                self.assertFalse(updater.poll(), 'An error must respect the next check interval')
                self.assertEqual(prepare.call_count, 1)

                updater.next_check = 0
                self.assertFalse(updater.poll())
                updater.worker.join(2)
                self.assertEqual(current.read_bytes(), b'# current\n', 'Worker must not install or stop the agent')
                self.assertTrue(updater.poll())
                self.assertEqual(current.read_bytes(), release['source'])
                self.assertEqual(json.loads((root / 'agent-version.json').read_text())['version'], 'test')
                self.assertIsNone(updater.worker)
                self.assertEqual(prepare.call_count, 2)

    def test_committed_update_restarts_even_if_version_marker_cannot_be_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            node.install_guard(root, b'# current\n', GUARD_SOURCE)
            current = root / 'node-agent.py'
            release = {'source': b'# verified replacement\n', 'version': 'test'}
            original_atomic = node.atomic

            def save(path, *args):
                if path.name == 'agent-version.json':
                    raise PermissionError('marker is read-only')
                return original_atomic(path, *args)

            with patch.object(node, 'atomic', side_effect=save), patch('builtins.print'):
                self.assertTrue(node.apply_update(root, release))
            self.assertEqual(current.read_bytes(), release['source'])

    def test_supervisor_restart_update_and_child_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = SOURCE.read_text().replace('UPDATE_INTERVAL = 600', 'UPDATE_INTERVAL = 0.2')
            source = source.replace('RESTART_DELAY = 2', 'RESTART_DELAY = 0.1').replace('time.sleep(10)', 'time.sleep(0.1)')
            (root / 'node.py').write_text(source)
            state = {'source': source.encode(), 'heartbeats': 0}
            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *args): pass
                def do_GET(self):
                    body = (state['source'] if self.path == '/node.py' else GUARD_SOURCE if self.path == '/node-supervisor.py' else json.dumps({
                        'url': '/node.py', 'sha256': hashlib.sha256(state['source']).hexdigest(), 'version': 'test',
                        'supervisor': {'url': '/node-supervisor.py', 'sha256': hashlib.sha256(GUARD_SOURCE).hexdigest()}}).encode())
                    self.send_response(200); self.end_headers(); self.wfile.write(body)
                def do_POST(self):
                    self.rfile.read(int(self.headers['Content-Length']))
                    state['heartbeats'] += 1
                    self.send_response(200); self.end_headers(); self.wfile.write(b'{"ok":true}')
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cfg = {'hub': 'http://127.0.0.1:' + str(server.server_port), 'id': 'test-node', 'token': 'test-only'}
            (root / 'config.json').write_text(json.dumps(cfg))
            (root / 'frpc.toml').write_text('test')
            (root / 'frpc').write_text('#!' + sys.executable + '\nimport os,pathlib,time\n'
                                      'root=pathlib.Path(__file__).parent\n'
                                      '(root/"frpc-test.pid").write_text(str(os.getpid()))\n'
                                      'while True: time.sleep(1)\n')
            (root / 'frpc').chmod(0o700)
            process = None
            seen = []
            with (root / 'log').open('w') as log:
                try:
                    command = [sys.executable, str(root / 'node.py'), 'supervise', '--root', str(root)]
                    process = subprocess.Popen(command, stdout=log, stderr=log)
                    wait_for(lambda: state['heartbeats'] > 0 and (root / 'guard.pid').exists() and
                             (root / 'run.pid').exists() and (root / 'frpc-test.pid').exists() and
                             active(int((root / 'frpc-test.pid').read_text())))
                    first_run = int((root / 'run.pid').read_text())
                    first_frp = int((root / 'frpc-test.pid').read_text()); seen.append(first_frp)
                    # Restart FRP in a runner which already has metrics threads.
                    os.kill(first_frp, signal.SIGKILL)
                    wait_for(lambda: int((root / 'frpc-test.pid').read_text()) != first_frp)
                    self.assertEqual(int((root / 'run.pid').read_text()), first_run)
                    self.assertFalse(active(first_frp))
                    first_frp = int((root / 'frpc-test.pid').read_text()); seen.append(first_frp)
                    duplicate = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
                    self.assertNotEqual(duplicate.returncode, 0)
                    self.assertTrue(active(first_run))
                    os.kill(first_run, signal.SIGKILL)
                    wait_for(lambda: (root / 'run.pid').exists() and int((root / 'run.pid').read_text()) != first_run)
                    wait_for(lambda: int((root / 'frpc-test.pid').read_text()) != first_frp)
                    self.assertFalse(active(first_frp), 'FRP must die when its runner is killed')
                    second_run = int((root / 'run.pid').read_text())
                    second_frp = int((root / 'frpc-test.pid').read_text()); seen.append(second_frp)
                    state['source'] += b'\n# upgraded-release\n'
                    wait_for(lambda: (root / 'node-agent.py').read_bytes() == state['source'])
                    wait_for(lambda: (root / 'run.pid').exists() and int((root / 'run.pid').read_text()) != second_run)
                    wait_for(lambda: int((root / 'frpc-test.pid').read_text()) != second_frp)
                    self.assertFalse(active(second_frp), 'Old FRP must be reaped during agent update')
                    seen.append(int((root / 'frpc-test.pid').read_text()))
                    self.assertEqual(json.loads((root / 'config.json').read_text()), cfg)
                    process.terminate()
                    self.assertEqual(process.wait(timeout=10), 0)
                    wait_for(lambda: not any(active(pid) for pid in seen))
                    self.assertFalse((root / 'run.pid').exists())
                    self.assertFalse((root / 'supervise.pid').exists())
                except BaseException:
                    print((root / 'log').read_text(), file=sys.stderr)
                    raise
                finally:
                    if process and process.poll() is None:
                        process.terminate(); process.wait(timeout=10)
                    server.shutdown(); server.server_close(); thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
