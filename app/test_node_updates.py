"""Guarded updates use real child processes and synthetic local configuration only."""
import contextlib
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).parent / 'static/node.py'
spec = importlib.util.spec_from_file_location('fleet_update_tested', SOURCE)
node = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node)
GUARD = SOURCE.with_name('node-supervisor.py').read_text()
FAST_GUARD = (GUARD.replace('PROBATION_SECONDS = 60', 'PROBATION_SECONDS = 0.3')
              .replace('STARTUP_TIMEOUT = 90', 'STARTUP_TIMEOUT = 1')
              .replace('LOOP_MAX_AGE = 35', 'LOOP_MAX_AGE = 0.5')
              .replace('RESTART_DELAY = 2', 'RESTART_DELAY = 0.05')
              .replace('time.sleep(.5)', 'time.sleep(.05)')).encode()
GOOD = b'''import os, pathlib, time
root = pathlib.Path(__file__).parent
(root / 'fixture.pid').write_text(str(os.getpid()))
fd = os.environ.get('FLEET_AGENT_READY_FD')
while True:
    if fd:
        try:
            os.write(int(fd), b'.')
        except BrokenPipeError:
            os.close(int(fd))
            fd = None
    time.sleep(.05)
'''


def wait_for(predicate, timeout=5):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError('Timed out waiting for guarded agent')


@contextlib.contextmanager
def running(root):
    with (root / 'fixture.log').open('w') as log:
        child = subprocess.Popen([sys.executable, str(root / 'node.py'), 'run', '--root', str(root)],
                                 stdout=log, stderr=log)
        try:
            yield child
        except BaseException:
            print((root / 'fixture.log').read_text(), file=sys.stderr)
            raise
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=7)


class GuardedUpdateTests(unittest.TestCase):
    def test_real_bad_releases_roll_back_and_are_not_downloaded_again(self):
        variants = {
            'import_failure': b"raise RuntimeError('synthetic import failure')\n",
            'early_exit': b'import time\ntime.sleep(.1)\n',
            'startup_hang': b'import time\ntime.sleep(60)\n',
            'health_revoked': (b'import os,time\nfd=int(os.environ["FLEET_AGENT_READY_FD"])\n'
                               b'os.write(fd,b".")\ntime.sleep(.1)\n'
                               b'while True:\n    os.write(fd,b"!")\n    time.sleep(.05)\n'),
            # A corrupted installed candidate is handled even before node code can run.
            'syntax_failure': b'def broken(\n',
        }
        for name, candidate in variants.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                node.install_guard(root, GOOD, FAST_GUARD)
                credentials = b'{"id":"synthetic-same-id","token":"synthetic-only"}'
                (root / 'config.json').write_bytes(credentials)
                node.apply_update(root, {'source': candidate, 'version': name})
                (root / 'agent-ready.json').write_text('{"ready":true}')
                with running(root):
                    wait_for(lambda: node.update_state(root).get('last_result') == 'rolled_back')
                    wait_for(lambda: (root / 'fixture.pid').exists())
                    self.assertEqual((root / 'node-agent.py').read_bytes(), GOOD)
                    self.assertEqual((root / 'node.py').read_bytes(), node.BOOTSTRAP)
                    self.assertEqual((root / 'config.json').read_bytes(), credentials)
                    expected = hashlib.sha256(candidate).hexdigest()
                    self.assertIn(expected, node.update_state(root)['rejected'])
                    with patch.object(node, 'request', return_value={'sha256': expected, 'url': '/node.py'}), \
                            patch.object(node.urllib.request, 'urlopen') as download:
                        self.assertIsNone(node.prepare_update(root, {'hub': 'https://synthetic.invalid', 'token': 'fixture'}))
                    download.assert_not_called()

    def test_local_loop_confirms_candidate_without_network_and_old_ready_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            node.install_guard(root, GOOD, FAST_GUARD)
            candidate = GOOD + b'\n# next release\n'
            node.apply_update(root, {'source': candidate, 'version': 'healthy'})
            (root / 'agent-ready.json').write_text('{"ready":true}')
            with running(root) as process:
                wait_for(lambda: node.update_state(root).get('last_result') == 'confirmed')
                self.assertIsNone(process.poll())
                self.assertEqual((root / 'node-agent.py').read_bytes(), candidate)
                self.assertIsNone(node.update_state(root)['pending'])
                # The local readiness pipe is closed after probation, so the agent
                # remains healthy when its next write raises BrokenPipeError.
                time.sleep(.15)
                self.assertIsNone(process.poll())

    def test_interrupted_install_before_source_replace_keeps_previous_release(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            node.install_guard(root, GOOD, FAST_GUARD)
            candidate = GOOD + b'\n# next release\n'
            node.apply_update(root, {'source': candidate, 'version': 'next'})
            (root / 'node-agent.py').write_bytes(GOOD)
            with running(root):
                wait_for(lambda: (root / 'fixture.pid').exists())
                self.assertIsNone(node.update_state(root)['pending'])
                self.assertFalse(node.update_state(root).get('rejected'))
                self.assertEqual((root / 'node-agent.py').read_bytes(), GOOD)

    def test_interrupted_rollback_finishes_rejection_after_previous_source_was_restored(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            node.install_guard(root, GOOD, FAST_GUARD)
            candidate = b'raise RuntimeError("synthetic broken release")\n'
            node.apply_update(root, {'source': candidate, 'version': 'broken'})
            saved = node.update_state(root)
            saved['pending']['rollback_reason'] = 'early_exit_1'
            (root / 'agent-update.json').write_text(json.dumps(saved))
            (root / 'node-agent.py').write_bytes(GOOD)
            with running(root):
                wait_for(lambda: node.update_state(root).get('last_result') == 'rolled_back')
                wait_for(lambda: (root / 'fixture.pid').exists())
                self.assertIn(hashlib.sha256(candidate).hexdigest(), node.update_state(root)['rejected'])
                self.assertIsNone(node.update_state(root)['pending'])
                self.assertEqual((root / 'node-agent.py').read_bytes(), GOOD)

    def test_partial_initial_layout_with_missing_launcher_can_be_reinstalled(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'node-supervisor.py').write_bytes(FAST_GUARD)
            self.assertFalse(node.guarded(root))
            node.install_guard(root, GOOD, FAST_GUARD)
            self.assertTrue(node.guarded(root))
            self.assertEqual(node.agent_path(root).read_bytes(), GOOD)

    def test_partial_migration_does_not_treat_staged_source_as_the_running_agent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'node.py').write_bytes(GOOD)
            (root / 'node-supervisor.py').write_bytes(FAST_GUARD)
            (root / 'node-agent.py').write_bytes(b'# incomplete staging from an interrupted installation\n')
            self.assertEqual(node.agent_path(root), root / 'node.py')
            self.assertEqual(node.agent_path(root).read_bytes(), GOOD)

    def test_real_agent_heartbeat_outage_does_not_reject_a_healthy_update(self):
        requests = []

        class UnavailableHub(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                requests.append(self.path)
                self.send_error(503, 'synthetic hub outage')

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), UnavailableHub)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                candidate = (SOURCE.read_text().replace('time.sleep(10)', 'time.sleep(.05)')
                             .replace('FRP_READY_SECONDS = 5', 'FRP_READY_SECONDS = 0.1')).encode()
                node.install_guard(root, GOOD, FAST_GUARD)
                node.apply_update(root, {'source': candidate, 'version': 'offline-healthy'})
                config = {'hub': 'http://127.0.0.1:' + str(server.server_port),
                          'id': 'synthetic-node', 'token': 'synthetic-token'}
                (root / 'config.json').write_text(json.dumps(config))
                (root / 'frpc.toml').write_text('# synthetic')
                (root / 'frpc').write_text('#!' + sys.executable + '\nimport time\ntime.sleep(60)\n')
                (root / 'frpc').chmod(0o700)
                with running(root):
                    wait_for(lambda: node.update_state(root).get('last_result') == 'confirmed')
                    self.assertTrue(requests, 'Exercise a real failing heartbeat, not a stubbed success')
                    self.assertFalse(node.update_state(root).get('rejected'))
                    self.assertEqual((root / 'node-agent.py').read_bytes(), candidate)
                    self.assertEqual(json.loads((root / 'config.json').read_text()), config)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_candidate_with_repeated_frp_exit_is_rolled_back(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            candidate = (SOURCE.read_text().replace('time.sleep(10)', 'time.sleep(.05)')
                         .replace('FRP_READY_SECONDS = 5', 'FRP_READY_SECONDS = 0.1')
                         .replace('response = latency.send(cfg, payload)', 'response = {"ok": True}')).encode()
            node.install_guard(root, GOOD, FAST_GUARD)
            node.apply_update(root, {'source': candidate, 'version': 'crashing-frp'})
            (root / 'config.json').write_text(json.dumps({'hub': 'https://synthetic.invalid',
                                                        'id': 'synthetic', 'token': 'synthetic'}))
            (root / 'frpc.toml').write_text('# synthetic')
            (root / 'frpc').write_text('#!' + sys.executable + '\nraise SystemExit(1)\n')
            (root / 'frpc').chmod(0o700)
            with running(root):
                wait_for(lambda: node.update_state(root).get('last_result') == 'rolled_back')
                self.assertEqual(node.update_state(root)['reason'], 'startup_timeout')
                self.assertEqual((root / 'node-agent.py').read_bytes(), GOOD)

    def test_blocked_heartbeat_does_not_decide_local_candidate_health(self):
        for name, frp_source, expected in (
            ('stable_frp', 'import time\ntime.sleep(60)\n', 'confirmed'),
            ('frp_exits_during_blocked_heartbeat', 'import time\ntime.sleep(.15)\n', 'rolled_back'),
        ):
            with self.subTest(name=name):
                entered, release, returned = threading.Event(), threading.Event(), threading.Event()

                class SlowHub(http.server.BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass

                    def do_POST(self):
                        self.rfile.read(int(self.headers['Content-Length']))
                        entered.set()
                        release.wait(4)
                        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                            self.send_response(200)
                            self.end_headers()
                            self.wfile.write(b'{"ok":true}')
                        returned.set()

                server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), SlowHub)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    with tempfile.TemporaryDirectory() as folder:
                        root = Path(folder)
                        candidate = (SOURCE.read_text().replace('time.sleep(10)', 'time.sleep(.05)')
                                     .replace('FRP_READY_SECONDS = 5', 'FRP_READY_SECONDS = 0.1')).encode()
                        node.install_guard(root, GOOD, FAST_GUARD)
                        node.apply_update(root, {'source': candidate, 'version': name})
                        (root / 'config.json').write_text(json.dumps({
                            'hub': 'http://127.0.0.1:' + str(server.server_port),
                            'id': 'synthetic', 'token': 'synthetic'}))
                        (root / 'frpc.toml').write_text('# synthetic')
                        (root / 'frpc').write_text('#!' + sys.executable + '\n' + frp_source)
                        (root / 'frpc').chmod(0o700)
                        with running(root):
                            self.assertTrue(entered.wait(2), 'The candidate must be blocked in a real HTTP request')
                            started = time.monotonic()
                            wait_for(lambda: node.update_state(root).get('last_result') == expected)
                            # Keep the real request blocked beyond the guard's
                            # shortened 1 s startup deadline, even after confirmation.
                            remaining = 1.1 - (time.monotonic() - started)
                            if remaining > 0:
                                time.sleep(remaining)
                            self.assertFalse(returned.is_set())
                            self.assertEqual(node.update_state(root)['last_result'], expected)
                            if expected == 'confirmed':
                                self.assertFalse(node.update_state(root).get('rejected'))
                            else:
                                self.assertEqual(node.update_state(root)['reason'], 'startup_timeout')
                finally:
                    release.set()
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

    def test_real_agent_local_initialization_hang_is_still_rolled_back(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            candidate = SOURCE.read_text().replace('resources, gpu = MetricsCollector(), GPUCollector()',
                                                   'time.sleep(60)\n        resources, gpu = MetricsCollector(), GPUCollector()').encode()
            node.install_guard(root, GOOD, FAST_GUARD)
            node.apply_update(root, {'source': candidate, 'version': 'initialization_hang'})
            (root / 'config.json').write_text(json.dumps({'hub': 'https://synthetic.invalid',
                                                        'id': 'synthetic', 'token': 'synthetic'}))
            with running(root):
                wait_for(lambda: node.update_state(root).get('last_result') == 'rolled_back')
                self.assertEqual(node.update_state(root)['reason'], 'startup_timeout')
                self.assertEqual((root / 'node-agent.py').read_bytes(), GOOD)

    def test_readiness_reporter_closes_and_stops_when_guard_confirms(self):
        read_fd, write_fd = os.pipe()
        reporter = node.CandidateReadiness(write_fd)
        child = type('StableChild', (), {'poll': lambda self: None})()
        try:
            reporter.watch(child, time.monotonic() - node.FRP_READY_SECONDS)
            self.assertEqual(os.read(read_fd, 1), b'.')
            os.close(read_fd)
            read_fd = None
            wait_for(lambda: not reporter.worker.is_alive(), timeout=2)
            self.assertIsNone(reporter.fd)
            self.assertTrue(reporter.stopped.is_set())
            # Closing twice must not close a newly reused OS descriptor.
            next_read, next_write = os.pipe()
            try:
                reporter.stop()
                reporter.stop()
                os.write(next_write, b'x')
                self.assertEqual(os.read(next_read, 1), b'x')
            finally:
                os.close(next_read)
                os.close(next_write)
        finally:
            reporter.stop()
            if read_fd is not None:
                os.close(read_fd)

    def test_prepared_same_release_does_not_download_or_restart_and_pending_blocks_next_update(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            node.install_guard(root, GOOD, FAST_GUARD)
            release = {'sha256': hashlib.sha256(GOOD).hexdigest(), 'url': '/node.py'}
            with patch.object(node, 'request', return_value=release), patch.object(node.urllib.request, 'urlopen') as download:
                self.assertIsNone(node.prepare_update(root, {'hub': 'https://synthetic.invalid', 'token': 'fixture'}))
            download.assert_not_called()
            node.apply_update(root, {'source': GOOD + b'\n# changed\n', 'version': 'next'})
            with self.assertRaisesRegex(ValueError, 'still being verified'):
                node.apply_update(root, {'source': GOOD, 'version': 'other'})

    def test_failed_candidate_write_keeps_current_source_and_clears_pending(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            node.install_guard(root, GOOD, FAST_GUARD)
            original = node.atomic

            def disk_full(path, content, *args):
                if path.name == 'node-agent.py':
                    raise OSError('synthetic full disk')
                original(path, content, *args)

            with patch.object(node, 'atomic', side_effect=disk_full):
                with self.assertRaises(OSError):
                    node.apply_update(root, {'source': GOOD + b'\n# new\n', 'version': 'new'})
            self.assertEqual((root / 'node-agent.py').read_bytes(), GOOD)
            self.assertIsNone(node.update_state(root)['pending'])

    def test_prepared_update_waits_for_transfer_and_recovers_without_redownload(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            node.install_guard(root, GOOD, FAST_GUARD)
            release = {'source': GOOD + b'\n# next\n', 'version': 'next'}
            updater = node.AgentUpdater(root, {})
            updater.next_check = 0
            with patch.object(node, 'prepare_update', return_value=release) as prepare:
                self.assertFalse(updater.poll(allow_apply=False))
                updater.worker.join(2)
                self.assertFalse(updater.poll(allow_apply=False))
                self.assertEqual(updater.status()['status'], 'deferred')
                self.assertEqual((root / 'node-agent.py').read_bytes(), GOOD)
                for _ in range(3):
                    self.assertFalse(updater.poll(allow_apply=False))
                prepare.assert_called_once()
                self.assertTrue(updater.poll(allow_apply=True))
                self.assertEqual((root / 'node-agent.py').read_bytes(), release['source'])
                prepare.assert_called_once()


if __name__ == '__main__':
    unittest.main()
