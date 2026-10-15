"""Direct transfer connection, timing, and HTTP budgets; no network or real waits."""
import importlib.util
from pathlib import Path
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
            now[0] += kwargs['timeout']
            if now[0] < 75:
                raise OSError('direct tunnel is not ready')
            connected.append(self)

        def get_transport(self):
            return SimpleNamespace(set_keepalive=lambda seconds: None)

        def close(self):
            pass

    with patch.object(transfer, 'host_key', return_value=pinned), \
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
