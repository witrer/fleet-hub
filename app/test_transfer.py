"""Direct transfer connection budget regression; no network or real waits."""
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
