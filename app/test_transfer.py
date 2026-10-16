"""Direct transfer connection, timing, and HTTP budgets; no network or real waits."""
import importlib.util
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest


spec = importlib.util.spec_from_file_location('fleet_transfer_tested', Path(__file__).parent / 'static/transfer.py')
transfer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer)


@pytest.mark.parametrize('budget, succeeds', [(60, False), (90, True), (180, True)])
def test_connection_budget_allows_hole_punching_beyond_one_minute(tmp_path, budget, succeeds):
    now = [0.0]
    connected = []
    log = tmp_path / 'frpc.log'
    log.write_text('nathole prepare success\n')
    pinned = SimpleNamespace(get_name=lambda: 'ssh-ed25519')

    class Client:
        def get_host_keys(self):
            return SimpleNamespace(add=lambda *args: None)

        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, *args, **kwargs):
            # A usable direct path appears only after later FRP strategies run.
            now[0] += min(20, kwargs['timeout'])
            if now[0] < 75:
                raise OSError('direct tunnel is not ready')
            connected.append(self)

        def get_transport(self):
            return SimpleNamespace(set_keepalive=lambda seconds: None)

        def close(self):
            pass

    with patch.object(transfer, 'host_key', return_value=pinned), \
         patch.object(transfer.socket, 'create_connection', side_effect=lambda *a, **k: SimpleNamespace(close=lambda: None)), \
         patch.object(transfer.paramiko, 'SSHClient', Client), \
         patch.object(transfer.time, 'monotonic', side_effect=lambda: now[0]), \
         patch.object(transfer.time, 'sleep', side_effect=lambda seconds: now.__setitem__(0, now[0] + seconds)):
        args = ({'ssh_host_key': 'test', 'ssh_user': 'root'}, None, 12345,
                SimpleNamespace(poll=lambda: None), log)
        if succeeds:
            assert transfer.connect_direct(*args, connect_timeout=budget) is connected[0]
        else:
            with pytest.raises(RuntimeError, match='60 seconds waiting for P2P UDP hole punching'):
                transfer.connect_direct(*args, connect_timeout=budget)
            assert not connected


def test_one_ssh_attempt_survives_frp_twenty_second_tunnel_window(tmp_path):
    now = [0.0]
    log = tmp_path / 'frpc.log'
    log.write_text('')
    client = SimpleNamespace(
        get_host_keys=lambda: SimpleNamespace(add=lambda *a: None),
        set_missing_host_key_policy=lambda *a: None,
        get_transport=lambda: SimpleNamespace(set_keepalive=lambda *a: None),
        close=lambda: pytest.fail('Successful connection was closed'),
    )

    def connect(*args, **kwargs):
        assert kwargs['sock'] is connection
        assert kwargs['timeout'] == kwargs['banner_timeout'] == 30
        assert kwargs['channel_timeout'] == 60
        assert kwargs['allow_agent'] is kwargs['look_for_keys'] is False
        now[0] += 21  # FRP's 20s window plus SSH negotiation.

    client.connect = connect
    connection = SimpleNamespace(close=lambda: pytest.fail('Successful socket was closed'))
    with patch.object(transfer, 'host_key', return_value=SimpleNamespace(get_name=lambda: 'ssh-ed25519')), \
         patch.object(transfer.paramiko, 'SSHClient', return_value=client) as factory, \
         patch.object(transfer.socket, 'create_connection', return_value=connection) as local_connect, \
         patch.object(transfer.time, 'monotonic', side_effect=lambda: now[0]), \
         patch.object(transfer.time, 'sleep') as sleep:
        assert transfer.connect_direct({'ssh_host_key': 'test', 'ssh_user': 'root'}, None, 12345,
                                       SimpleNamespace(poll=lambda: None), log) is client
    factory.assert_called_once()
    local_connect.assert_called_once_with(('127.0.0.1', 12345), timeout=5)
    sleep.assert_not_called()


@pytest.mark.parametrize('failure,reason', [
    (transfer.paramiko.AuthenticationException('synthetic rejection'), 'SSH authentication failed'),
    (transfer.paramiko.BadHostKeyException('localhost', None, None), 'SSH host key did not match'),
])
def test_identity_failures_stop_without_retry_and_close_socket(tmp_path, failure, reason):
    log = tmp_path / 'frpc.log'
    log.write_text('')
    closed = []
    client = SimpleNamespace(
        get_host_keys=lambda: SimpleNamespace(add=lambda *a: None),
        set_missing_host_key_policy=lambda *a: None,
        get_transport=lambda: SimpleNamespace(remote_version='SSH-2.0-test'),
        close=lambda: closed.append('client'),
    )

    def connect(*args, **kwargs):
        raise failure

    client.connect = connect
    connection = SimpleNamespace(close=lambda: closed.append('socket'))
    with patch.object(transfer, 'host_key', return_value=SimpleNamespace(get_name=lambda: 'ssh-ed25519')), \
         patch.object(transfer.paramiko, 'SSHClient', return_value=client) as factory, \
         patch.object(transfer.socket, 'create_connection', return_value=connection), \
         patch.object(transfer.time, 'sleep') as sleep:
        with pytest.raises(RuntimeError, match=reason):
            transfer.connect_direct({'ssh_host_key': 'test', 'ssh_user': 'root'}, None, 12345,
                                    SimpleNamespace(poll=lambda: None), log)
    assert closed == ['client', 'socket']
    factory.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize('log_text,remote_version,stage', [
    ('nathole prepare success\n', '', 'P2P UDP hole punching'),
    ('establishing nat hole connection successful\ninit tunnel session error\n', '',
     'P2P tunnel session establishment'),
    ('', 'SSH-2.0-test', 'SSH handshake/authentication over the direct tunnel'),
])
def test_timeout_stage_requires_actual_ssh_evidence(tmp_path, log_text, remote_version, stage):
    now = [0.0]
    log = tmp_path / 'frpc.log'
    log.write_text(log_text)

    def connect(*args, **kwargs):
        now[0] += kwargs['timeout']
        raise transfer.paramiko.SSHException('Connection closed')

    client = SimpleNamespace(
        get_host_keys=lambda: SimpleNamespace(add=lambda *a: None),
        set_missing_host_key_policy=lambda *a: None,
        get_transport=lambda: SimpleNamespace(remote_version=remote_version),
        close=lambda: None, connect=connect,
    )
    with patch.object(transfer, 'host_key', return_value=SimpleNamespace(get_name=lambda: 'ssh-ed25519')), \
         patch.object(transfer.paramiko, 'SSHClient', return_value=client), \
         patch.object(transfer.socket, 'create_connection', return_value=SimpleNamespace(close=lambda: None)), \
         patch.object(transfer.time, 'monotonic', side_effect=lambda: now[0]), \
         patch.object(transfer.time, 'sleep') as sleep:
        with pytest.raises(RuntimeError, match=stage):
            transfer.connect_direct({'ssh_host_key': 'test', 'ssh_user': 'root'}, None, 12345,
                                    SimpleNamespace(poll=lambda: None), log, connect_timeout=1)
    sleep.assert_not_called()  # No extra retry delay after the deadline.


@pytest.mark.parametrize('stall_stage', ['banner', 'authentication'])
def test_real_loopback_stalls_respect_total_connection_deadline(tmp_path, stall_stage):
    """Exercise real Paramiko threads and sockets; no FRP, hub, or external data."""
    log = tmp_path / 'frpc.log'
    log.write_text('')
    release = threading.Event()
    entered_auth = threading.Event()
    server_errors = []
    server_key = transfer.paramiko.RSAKey.generate(1024)
    user_key = transfer.paramiko.RSAKey.generate(1024)

    class Server(transfer.paramiko.ServerInterface):
        def get_allowed_auths(self, username):
            return 'publickey'

        def check_auth_publickey(self, username, key):
            entered_auth.set()
            release.wait(2)
            return transfer.paramiko.AUTH_SUCCESSFUL

    def accept():
        transport = None
        try:
            connection, _ = listener.accept()
            with connection:
                if stall_stage == 'banner':
                    release.wait(2)
                else:
                    # Consume some of the same budget before authentication.
                    time.sleep(0.15)
                    transport = transfer.paramiko.Transport(connection)
                    transport.add_server_key(server_key)
                    transport.start_server(server=Server())
                    release.wait(2)
        except Exception as exc:
            server_errors.append(exc)
        finally:
            if transport is not None:
                transport.close()

    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        thread = threading.Thread(target=accept, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with pytest.raises(RuntimeError, match='Timed out after 0.4 seconds'):
                transfer.connect_direct({'ssh_host_key': server_key.get_name() + ' ' + server_key.get_base64(),
                                         'ssh_user': 'synthetic-test'}, user_key, listener.getsockname()[1],
                                        SimpleNamespace(poll=lambda: None), log, connect_timeout=0.4)
            elapsed = time.monotonic() - started
            assert 0.35 <= elapsed < 0.65
            if stall_stage == 'authentication':
                assert entered_auth.is_set()
        finally:
            release.set()
            thread.join(timeout=2)
    assert not thread.is_alive()
    assert not server_errors


def test_frpc_exit_does_not_include_raw_log_contents(tmp_path):
    log = tmp_path / 'frpc.log'
    log.write_text('synthetic-sensitive-value')
    with patch.object(transfer, 'host_key'), pytest.raises(RuntimeError) as error:
        transfer.connect_direct({'ssh_host_key': 'test'}, None, 12345, SimpleNamespace(poll=lambda: 1), log)
    assert 'exit code 1' in str(error.value)
    assert 'synthetic-sensitive-value' not in str(error.value)


def test_log_tail_reads_only_bounded_suffix(tmp_path):
    log = tmp_path / 'frpc.log'
    log.write_bytes(b'x' * 50000 + b'end')
    with patch.object(Path, 'read_text', side_effect=AssertionError('Unbounded text read')):
        assert transfer.log_tail(log, limit=10) == 'xxxxxxxend'


def test_cached_frpc_hash_does_not_read_whole_executable(tmp_path):
    checksum = 'a' * 64
    cache = tmp_path / 'fleet-hub/frp' / ('0.71.1-linux_amd64-' + checksum[:12])
    cache.mkdir(parents=True)
    binary = cache / 'frpc'
    content = b'bounded-memory-hash' * 100000
    binary.write_bytes(content)
    (cache / 'binary.sha256').write_text(transfer.hashlib.sha256(content).hexdigest())
    with patch.dict(transfer.os.environ, {'LOCALAPPDATA': str(tmp_path)}), \
         patch.object(Path, 'read_bytes', side_effect=AssertionError('Unbounded binary read')), \
         patch.object(transfer.urllib.request, 'urlopen', side_effect=AssertionError('Unnecessary download')):
        assert transfer.frpc_binary({'frp_version': '0.71.1', 'sha256': checksum}, 'linux_amd64') == binary


def test_active_speed_excludes_tool_and_tunnel_preparation():
    now = [0.0]
    with patch.object(transfer.time, 'monotonic', side_effect=lambda: now[0]):
        progress = transfer.Progress('https://hub.invalid', {'id': 'job', 'token': 'test-token'}, 10_000_000)
        assert progress.payload()['phase'] == 'preparing_tool'
        now[0] = 30
        progress.update(phase='connecting')
        assert progress.payload()['speed_bps'] == 0
        assert progress.payload()['transfer_seconds'] == 0
        now[0] = 180
        progress.update(status='transferring')
        now[0] = 190
        progress.update(completed=10_000_000)
        payload = progress.payload()
        assert payload['phase'] == 'transferring'
        assert payload['elapsed_seconds'] == 190
        assert payload['transfer_seconds'] == 10
        assert payload['speed_bps'] == 1_000_000
        # Repeating the same status must not restart the active transfer clock.
        now[0] = 200
        progress.update(status='transferring')
        assert progress.payload()['transfer_seconds'] == 20
        assert progress.payload()['speed_bps'] == 500_000


@pytest.mark.parametrize('status,error', [('completed', ''), ('failed', 'connection lost')])
def test_terminal_progress_freezes_before_monitoring_cleanup(status, error):
    now = [0.0]
    sent = []
    with patch.object(transfer.time, 'monotonic', side_effect=lambda: now[0]):
        progress = transfer.Progress('https://hub.invalid', {'id': 'job', 'token': 'test-token'}, 10_000_000)
        now[0] = 180
        progress.update(status='transferring')
        now[0] = 190
        progress.update(completed=10_000_000)
        progress.thread = SimpleNamespace(is_alive=lambda: True,
                                          join=lambda timeout: now.__setitem__(0, now[0] + timeout))
        with patch.object(progress, 'send', side_effect=lambda: sent.append(progress.payload())):
            progress.finish(status, error)
        now[0] = 250
        payload = progress.payload()
        assert sent == [payload]
        assert payload['elapsed_seconds'] == 190
        assert payload['transfer_seconds'] == 10
        assert payload['speed_bps'] == 1_000_000
        assert payload['status'] == payload['phase'] == status
        assert payload['error'] == ('transferring: connection lost' if error else '')


def test_connection_failure_has_zero_active_duration_and_preserves_failed_phase():
    now = [0.0]
    with patch.object(transfer.time, 'monotonic', side_effect=lambda: now[0]):
        progress = transfer.Progress('https://hub.invalid', {'id': 'job', 'token': 'test-token'}, 10_000_000)
        progress.update(phase='connecting')
        now[0] = 180
        with patch.object(progress, 'send'):
            progress.finish('failed', 'tunnel unavailable')
        payload = progress.payload()
        assert payload['elapsed_seconds'] == 180
        assert payload['bytes_done'] == payload['speed_bps'] == payload['transfer_seconds'] == 0
        assert payload['error'] == 'connecting: tunnel unavailable'


@pytest.mark.parametrize('kwargs,expected', [({}, 15), ({'timeout': 45}, 45)])
def test_api_passes_the_selected_http_timeout(kwargs, expected):
    with patch.object(transfer.urllib.request, 'urlopen', return_value=transfer.io.BytesIO(b'{}')) as request:
        assert transfer.api('https://hub.invalid', '/api/transfers', 'test-token', {}, **kwargs) == {}
    assert request.call_args.kwargs['timeout'] == expected


def test_task_creation_allows_server_ssh_preparation_budget(tmp_path):
    source = tmp_path / 'dataset.bin'
    source.write_bytes(b'synthetic')
    arguments = ['transfer.py', '--hub', 'https://hub.invalid', '--node', 'test-node',
                 '--source', str(source), '--destination', '/data/dataset.bin']
    with patch.object(transfer.sys, 'argv', arguments), \
         patch.dict(transfer.os.environ, {'FLEET_TOKEN': 'test-token'}), \
         patch.object(transfer, 'platform_name', return_value='linux_amd64'), \
         patch.object(transfer, 'api', side_effect=RuntimeError('stop after creating task')) as request:
        assert transfer.main() == 1
    assert request.call_args.args[1] == '/api/transfers'
    assert request.call_args.kwargs['timeout'] == 45


def test_progress_reporting_keeps_short_http_timeout():
    progress = transfer.Progress('https://hub.invalid', {'id': 'job', 'token': 'test-token'}, 1)
    with patch.object(transfer, 'api') as request:
        progress.send()
    assert request.call_args.args[1] == '/api/transfers/job/progress'
    assert request.call_args.kwargs['timeout'] == 4
