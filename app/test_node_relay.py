"""OneDrive receive checks use temporary files and loopback HTTP only."""
import contextlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.parse
import urllib.request


SOURCE = Path(__file__).parent / 'static/node.py'
spec = importlib.util.spec_from_file_location('fleet_node_relay_tested', SOURCE)
node = importlib.util.module_from_spec(spec)
spec.loader.exec_module(node)


class RelayHTTP:
    """Real HTTP bodies/redirects, with HTTPS transport mapped to loopback."""

    def __init__(self, destination, body=b'fixture dataset\n' * 4096):
        self.body = body
        self.task = {'id': 'task-1', 'destination': str(destination), 'overwrite': False,
                     'is_directory': False, 'directories': [], 'files': [
                         {'id': 'file-1', 'path': 'dataset.bin', 'size': len(body), 'etag': 'etag-v1',
                          'download_path': '/api/relay/transfers/task-1/files/file-1/content'}]}
        self.progress = []
        self.progress_errors = []
        self.request_timeouts = []
        self.clock = 0
        self.advance_clock = False
        self.storage_requests = []
        self.hub_authorization = []
        self.link_requests = 0
        self.disconnections = 0
        self.ignore_range = False
        self.expired_links = 0
        self.storage_error = None
        self.ack_errors = 0
        self.content_error = None
        self.changed_after_download = False
        self.bad_range = False
        self.redirect_location = None
        self.block_download = None
        self.download_started = threading.Event()
        fixture = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *_):
                pass

            def reply(self, status, value=None, headers=None):
                body = b'' if value is None else json.dumps(value).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith('/api/'):
                    fixture.hub_authorization.append(self.headers.get('Authorization'))
                    if self.path.endswith('/relay-task'):
                        return self.reply(200, {'task': fixture.task})
                    if self.path.endswith('/content'):
                        fixture.link_requests += 1
                        if fixture.content_error:
                            return self.reply(fixture.content_error, {'error': 'fixture'})
                        url = fixture.redirect_location or ('https://download.fixture.invalid/blob?signature=fixture-'
                                                            + str(fixture.link_requests))
                        return self.reply(302, headers={'Location': url})
                    return self.reply(404)
                fixture.storage_requests.append({'range': self.headers.get('Range'),
                                                 'authorization': self.headers.get('Authorization'),
                                                 'path': self.path})
                fixture.download_started.set()
                if fixture.block_download is not None:
                    fixture.block_download.wait(5)
                if fixture.expired_links:
                    fixture.expired_links -= 1
                    return self.reply(403)
                if fixture.storage_error:
                    return self.reply(fixture.storage_error, headers={'Retry-After': '60'})
                ranged = self.headers.get('Range') and not fixture.ignore_range
                offset = int(self.headers['Range'].split('=')[1].split('-')[0]) if ranged else 0
                body = fixture.body[offset:]
                self.send_response(206 if ranged else 200)
                self.send_header('Content-Length', str(len(body)))
                if ranged:
                    start = offset + 1 if fixture.bad_range else offset
                    self.send_header('Content-Range', f'bytes {start}-{len(fixture.body) - 1}/{len(fixture.body)}')
                self.end_headers()
                if fixture.disconnections:
                    fixture.disconnections -= 1
                    self.wfile.write(body[:37])
                    self.wfile.flush()
                    self.close_connection = True
                    with contextlib.suppress(OSError):
                        self.connection.shutdown(socket.SHUT_RDWR)
                else:
                    if fixture.changed_after_download:
                        fixture.content_error = 409
                    with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                        self.wfile.write(body)

            def do_POST(self):
                fixture.hub_authorization.append(self.headers.get('Authorization'))
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if self.path.endswith('/heartbeat'):
                    return self.reply(200, {'ok': True, 'relay_available': True, 'update_allowed': True})
                fixture.progress.append(payload)
                if payload['file_id'] and payload['status'] == 'downloading' and fixture.progress_errors:
                    return self.reply(fixture.progress_errors.pop(0), {'error': 'fixture'},
                                      headers={'Retry-After': '10'})
                if payload['file_id'] and payload['status'] == 'completed' and fixture.ack_errors:
                    fixture.ack_errors -= 1
                    return self.reply(503, {'error': 'fixture'})
                return self.reply(200, {'ok': True})

        self.hub = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.storage = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        for server in (self.hub, self.storage):
            server.daemon_threads = True
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.hub_url = 'http://127.0.0.1:' + str(self.hub.server_port)
        self.cfg = {'id': 'node-1', 'token': 'synthetic-node-credential', 'hub': self.hub_url}
        self.opener = urllib.request.build_opener(node.RelayNoRedirect())

    def open(self, request, timeout):
        parsed = urllib.parse.urlsplit(request.full_url)
        self.request_timeouts.append((parsed.path, timeout))
        if parsed.hostname == 'download.fixture.invalid' and parsed.scheme == 'https':
            request = urllib.request.Request('http://127.0.0.1:' + str(self.storage.server_port)
                                             + parsed.path + ('?' + parsed.query if parsed.query else ''),
                                             data=request.data, headers=dict(request.header_items()),
                                             method=request.get_method())
        elif not request.full_url.startswith(self.hub_url + '/'):
            raise AssertionError('Unexpected external network request')
        response = self.opener.open(request, timeout=timeout)
        if parsed.hostname == 'download.fixture.invalid' and self.advance_clock:
            fixture = self

            class TimedResponse:
                def __getattr__(self, name):
                    return getattr(response, name)

                def __enter__(self):
                    return self

                def __exit__(self, *_):
                    response.close()

                def read1(self, size):
                    if not 0 < size <= 1024 * 1024:
                        raise AssertionError('Unbounded download read')
                    chunk = response.read1(size)
                    if chunk:
                        fixture.clock += 6
                    return chunk

            return TimedResponse()
        return response

    def receiver(self, root):
        return node.RelayReceiver(root, self.cfg, open_url=self.open)

    def close(self):
        if self.block_download is not None:
            self.block_download.set()
        for server in (self.hub, self.storage):
            server.shutdown()
            server.server_close()


class RelayNodeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.target = self.root / 'received.bin'
        self.http = RelayHTTP(self.target)
        self.addCleanup(self.folder.cleanup)
        self.addCleanup(self.http.close)

    def receive(self):
        receiver = self.http.receiver(self.root)
        receiver._receive()
        return receiver

    def partial(self):
        self.http.disconnections = 1
        receiver = self.http.receiver(self.root)
        receiver.RETRIES = 1
        receiver._receive()
        self.assertEqual(receiver.last_error, 'download_interrupted')
        self.assertFalse(self.target.exists())
        self.assertEqual(next(self.root.glob('*.part')).read_bytes(), self.http.body[:37])
        return receiver

    def test_redirect_does_not_forward_token_and_ack_follows_durable_commit(self):
        receiver = self.receive()
        self.assertIsNone(receiver.last_error)
        self.assertEqual(self.target.read_bytes(), self.http.body)
        self.assertTrue(all(value == 'Bearer synthetic-node-credential' for value in self.http.hub_authorization))
        self.assertTrue(all(value['authorization'] is None for value in self.http.storage_requests))
        self.assertEqual(self.http.progress[-2], {'file_id': 'file-1', 'bytes_done': len(self.http.body), 'status': 'completed'})
        self.assertEqual(self.http.progress[-1]['status'], 'completed')
        saved = json.loads(next(self.root.glob('*.json')).read_text())
        self.assertEqual(saved['phase'], 'committed')
        self.assertNotIn('signature=fixture-', json.dumps(saved))
        self.assertEqual(list(self.root.glob('*.part')), [])

    def test_process_restart_resumes_partial_with_validated_range(self):
        self.partial()
        receiver = self.receive()
        self.assertIsNone(receiver.last_error)
        self.assertEqual(self.http.storage_requests[-1]['range'], 'bytes=37-')
        self.assertEqual(self.target.read_bytes(), self.http.body)
        self.assertFalse(any(progress['status'] == 'failed' for progress in self.http.progress))

    def test_range_ignored_200_restarts_instead_of_appending(self):
        self.partial()
        self.http.ignore_range = True
        self.assertIsNone(self.receive().last_error)
        self.assertEqual(self.target.read_bytes(), self.http.body)

    def test_expired_download_url_is_refreshed_without_token_leak(self):
        self.http.expired_links = 1
        self.assertIsNone(self.receive().last_error)
        self.assertEqual(self.http.link_requests, 3)
        self.assertNotEqual(self.http.storage_requests[0]['path'], self.http.storage_requests[1]['path'])
        self.assertTrue(all(value['authorization'] is None for value in self.http.storage_requests))

    def test_persistent_url_errors_are_bounded_and_retried_next_heartbeat(self):
        self.http.expired_links = 20
        receiver = self.receive()
        self.assertEqual(receiver.last_error, 'download_url_expired')
        self.assertEqual(len(self.http.storage_requests), receiver.RETRIES)
        self.assertFalse(any(progress['status'] == 'failed' for progress in self.http.progress))
        self.http.expired_links = 0
        self.assertIsNone(self.receive().last_error)

    def test_throttling_honors_retry_after_without_busy_retry_loop(self):
        self.http.storage_error = 429
        receiver = self.receive()
        self.assertEqual(receiver.last_error, 'download_failed')
        self.assertEqual(len(self.http.storage_requests), 1)
        self.assertGreater(receiver.next_retry, time.monotonic() + 50)
        receiver.poll(available=True)
        self.assertIsNone(receiver.worker)
        self.assertFalse(any(progress['status'] == 'failed' for progress in self.http.progress))

    def test_server_source_version_change_stops_before_downloading(self):
        self.http.content_error = 409
        receiver = self.receive()
        self.assertEqual(receiver.last_error, 'source_changed')
        self.assertEqual(self.http.storage_requests, [])
        self.assertEqual(self.http.progress[-1]['error'], 'source_changed')

    def test_source_changed_during_download_is_not_committed_or_acknowledged(self):
        self.http.changed_after_download = True
        receiver = self.receive()
        self.assertEqual(receiver.last_error, 'source_changed')
        self.assertEqual(len(self.http.storage_requests), 1)
        self.assertEqual(self.http.link_requests, 2)
        self.assertFalse(self.target.exists())
        self.assertEqual(next(self.root.glob('*.part')).read_bytes(), self.http.body)
        self.assertFalse(any(progress['status'] == 'completed' for progress in self.http.progress))

    def test_manifest_etag_change_does_not_reuse_old_partial(self):
        self.partial()
        self.http.task['files'][0]['etag'] = 'different-version'
        self.assertEqual(self.receive().last_error, 'source_changed')
        self.assertEqual(len(self.http.storage_requests), 1)
        self.assertFalse(self.target.exists())

    def test_bad_content_range_is_rejected_before_appending(self):
        self.partial()
        self.http.bad_range = True
        self.assertEqual(self.receive().last_error, 'invalid_download_range')
        self.assertEqual(next(self.root.glob('*.part')).read_bytes(), self.http.body[:37])

    def test_hub_revoked_task_stops_without_repeated_download(self):
        self.http.content_error = 410
        self.assertEqual(self.receive().last_error, 'task_unavailable')
        self.assertEqual(self.http.link_requests, 1)
        self.assertEqual(self.http.storage_requests, [])
        self.assertFalse(any(progress['status'] == 'failed' for progress in self.http.progress))

    def test_non_https_or_embedded_credentials_download_redirect_is_rejected(self):
        for url in ('http://download.fixture.invalid/file', 'https://user:pass@download.fixture.invalid/file'):
            with self.subTest(url=url):
                self.http.redirect_location = url
                self.assertEqual(self.receive().last_error, 'invalid_download_redirect')
                self.assertEqual(self.http.storage_requests, [])

    def test_ack_loss_then_restart_does_not_download_again(self):
        self.http.ack_errors = 1
        self.assertEqual(self.receive().last_error, 'relay_control_failed')
        self.assertEqual(self.target.read_bytes(), self.http.body)
        self.assertIsNone(self.receive().last_error)
        self.assertEqual(len(self.http.storage_requests), 1)

    def test_crash_after_rename_before_committed_marker_is_recoverable(self):
        self.assertIsNone(self.receive().last_error)
        marker = next(self.root.glob('*.json'))
        saved = json.loads(marker.read_text())
        saved['phase'] = 'ready'
        marker.write_text(json.dumps(saved))
        self.assertIsNone(self.receive().last_error)
        self.assertEqual(len(self.http.storage_requests), 1)
        self.assertEqual(json.loads(marker.read_text())['phase'], 'committed')

    def test_crash_between_no_overwrite_link_and_unlink_cleans_partial_name(self):
        self.assertIsNone(self.receive().last_error)
        marker = next(self.root.glob('*.json'))
        saved = json.loads(marker.read_text())
        saved['phase'] = 'ready'
        marker.write_text(json.dumps(saved))
        partial = marker.with_suffix('.part')
        os.link(self.target, partial)
        self.assertIsNone(self.receive().last_error)
        self.assertFalse(partial.exists())
        self.assertEqual(len(self.http.storage_requests), 1)

    def test_hardlinked_partial_is_not_written(self):
        self.partial()
        partial = next(self.root.glob('*.part'))
        linked = self.root / 'linked-copy'
        os.link(partial, linked)
        self.assertEqual(self.receive().last_error, 'unsafe_destination')
        self.assertEqual(linked.read_bytes(), self.http.body[:37])
        self.assertEqual(len(self.http.storage_requests), 1)

    def test_existing_same_size_target_is_not_mistaken_for_completed_transfer(self):
        self.target.write_bytes(b'x' * len(self.http.body))
        self.assertEqual(self.receive().last_error, 'destination_exists')
        self.assertEqual(self.target.read_bytes(), b'x' * len(self.http.body))
        self.assertEqual(self.http.storage_requests, [])

    def test_overwrite_uses_atomic_replacement(self):
        self.target.write_bytes(b'previous target')
        old_inode = self.target.stat().st_ino
        self.http.task['overwrite'] = True
        self.assertIsNone(self.receive().last_error)
        self.assertEqual(self.target.read_bytes(), self.http.body)
        self.assertNotEqual(self.target.stat().st_ino, old_inode)

    def test_changed_committed_target_is_not_reacknowledged(self):
        self.assertIsNone(self.receive().last_error)
        self.target.write_bytes(b'x' * len(self.http.body))
        self.assertEqual(self.receive().last_error, 'destination_changed')
        self.assertEqual(len(self.http.storage_requests), 1)

    def test_manifest_absolute_and_traversal_paths_are_rejected(self):
        for value in ('/absolute', '../escape', 'a/../../escape', 'a/./b', 'a//b', 'a\\b'):
            with self.subTest(path=value):
                self.http.task['files'][0]['path'] = value
                self.assertEqual(self.receive().last_error, 'unsafe_destination')
                self.assertEqual(self.http.storage_requests, [])

    def test_symlink_target_and_parent_are_rejected(self):
        external = self.root / 'external'
        external.write_bytes(b'unchanged')
        self.target.symlink_to(external)
        self.http.task['overwrite'] = True
        self.assertEqual(self.receive().last_error, 'unsafe_destination')
        self.target.unlink()
        parent = self.root / 'linked'
        parent.symlink_to(self.root, target_is_directory=True)
        self.http.task['destination'] = str(parent / 'received.bin')
        self.assertEqual(self.receive().last_error, 'destination_unavailable')
        self.assertEqual(external.read_bytes(), b'unchanged')
        self.assertEqual(self.http.storage_requests, [])

    def test_empty_directories_complete_with_task_level_ack(self):
        self.http.task.update(is_directory=True, files=[], directories=['', 'empty', 'nested/empty'])
        self.assertIsNone(self.receive().last_error)
        self.assertTrue((self.target / 'empty').is_dir())
        self.assertTrue((self.target / 'nested/empty').is_dir())
        self.assertEqual(self.http.storage_requests, [])
        self.assertEqual(self.http.progress[-1], {'file_id': None, 'bytes_done': 0, 'status': 'completed'})

    def test_empty_file_is_written_and_validated(self):
        self.http.body = b''
        self.http.task['files'][0]['size'] = 0
        self.assertIsNone(self.receive().last_error)
        self.assertEqual(self.target.read_bytes(), b'')
        self.assertEqual(self.http.link_requests, 2)

    def test_single_file_existing_directory_uses_source_name(self):
        self.target.mkdir()
        self.assertIsNone(self.receive().last_error)
        self.assertEqual((self.target / 'dataset.bin').read_bytes(), self.http.body)

    def test_server_completed_files_are_skipped(self):
        self.http.task['files'][0]['status'] = 'completed'
        self.assertIsNone(self.receive().last_error)
        self.assertEqual(self.http.storage_requests, [])

    def test_background_download_does_not_block_heartbeat_or_start_two_workers(self):
        self.http.block_download = threading.Event()
        receiver = self.http.receiver(self.root)
        receiver.poll(available=False)
        self.assertIsNone(receiver.worker)
        started = time.monotonic()
        receiver.poll(available=True)
        self.assertLess(time.monotonic() - started, .2)
        self.assertTrue(self.http.download_started.wait(2))
        worker = receiver.worker
        receiver.poll(available=True)
        self.assertIs(receiver.worker, worker)
        response = node.HeartbeatLatency().send(self.http.cfg, {})
        self.assertTrue(response['ok'])
        self.assertTrue(receiver.busy)
        self.http.block_download.set()
        worker.join(timeout=3)
        self.assertFalse(receiver.busy)
        self.assertIsNone(receiver.last_error)

    def test_fast_download_does_not_report_every_eight_megabytes(self):
        self.http.body = b'x' * (20 * 1024 * 1024)
        self.http.task['files'][0]['size'] = len(self.http.body)
        with patch.object(node.time, 'monotonic', return_value=1):
            receiver = self.receive()
        self.assertIsNone(receiver.last_error)
        self.assertEqual(self.target.stat().st_size, len(self.http.body))
        periodic = [value for value in self.http.progress if value['file_id'] and value['status'] == 'downloading']
        self.assertEqual(periodic, [])
        self.assertEqual(len(self.http.storage_requests), 1)

    def test_temporary_progress_failure_keeps_existing_stream_and_final_ack_is_strict(self):
        self.http.body = b'x' * (2 * 1024 * 1024)
        self.http.task['files'][0]['size'] = len(self.http.body)
        self.http.advance_clock = True
        self.http.progress_errors = [503]
        with patch.object(node.time, 'monotonic', side_effect=lambda: self.http.clock):
            receiver = self.receive()
        self.assertIsNone(receiver.last_error)
        self.assertEqual(self.http.progress_errors, [])
        self.assertEqual(self.target.read_bytes(), self.http.body)
        self.assertEqual(len(self.http.storage_requests), 1)
        progress_timeouts = [timeout for path, timeout in self.http.request_timeouts if path.endswith('/progress')]
        self.assertEqual(progress_timeouts[0], 20)
        self.assertTrue(all(timeout == 4 for timeout in progress_timeouts[1:-2]))
        self.assertEqual(progress_timeouts[-2:], [20, 20])
        self.assertGreaterEqual(len(progress_timeouts), 4)
        self.assertEqual(self.http.progress[-1]['status'], 'completed')

    def test_revocation_during_periodic_progress_still_stops_download(self):
        self.http.body = b'x' * (2 * 1024 * 1024)
        self.http.task['files'][0]['size'] = len(self.http.body)
        self.http.advance_clock = True
        self.http.progress_errors = [410]
        with patch.object(node.time, 'monotonic', side_effect=lambda: self.http.clock):
            receiver = self.receive()
        self.assertEqual(receiver.last_error, 'task_unavailable')
        self.assertFalse(self.target.exists())
        self.assertFalse(any(value['status'] == 'completed' for value in self.http.progress))
        self.assertEqual(len(self.http.storage_requests), 1)
        progress_timeouts = [timeout for path, timeout in self.http.request_timeouts if path.endswith('/progress')]
        self.assertEqual(progress_timeouts, [20, 4])

    def test_run_defers_update_while_relay_worker_is_active(self):
        (self.root / 'config.json').write_text(json.dumps(self.http.cfg))
        (self.root / 'node.py').write_bytes(b'synthetic installed source')
        updater = Mock()
        updater.status.return_value = {'status': 'idle'}
        updater.poll.return_value = False
        relay = Mock()
        relay.busy = True
        child = Mock()
        child.poll.return_value = None
        resources, gpu = Mock(), Mock()
        resources.snapshot.return_value = {}
        gpu.snapshot.return_value = {}
        latency = Mock()
        latency.send.return_value = {'ok': True, 'update_allowed': True, 'relay_available': True}
        with patch.object(node, 'MetricsCollector', return_value=resources), \
                patch.object(node, 'GPUCollector', return_value=gpu), \
                patch.object(node, 'AgentUpdater', return_value=updater), \
                patch.object(node, 'HeartbeatLatency', return_value=latency), \
                patch.object(node, 'RelayReceiver', return_value=relay), \
                patch.object(node, 'spawn_child', return_value=child), \
                patch.object(node.signal, 'signal'), \
                patch.object(node.time, 'sleep', side_effect=SystemExit), \
                patch.dict(os.environ, {}, clear=True), \
                self.assertRaises(SystemExit):
            node.run(SimpleNamespace(root=str(self.root)))
        relay.poll.assert_called_once_with(available=True)
        updater.poll.assert_called_once_with(allow_apply=False)
        self.assertEqual(latency.send.call_args.args[1]['relay_protocol'], 1)
        self.assertIs(type(latency.send.call_args.args[1]['relay_protocol']), int)
        relay.stop.assert_called_once()


if __name__ == '__main__':
    unittest.main()
