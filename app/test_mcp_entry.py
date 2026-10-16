"""Fleet tool contracts using synthetic config and a mocked loopback registry."""
import asyncio
import contextlib
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from relay_shell.config import Settings
from starlette.testclient import TestClient

REAL_HTTP_CLIENT = httpx.AsyncClient
NODE = 'node-0000000001'
OTHER = 'node-0000000002'
STATE = {'nodes': [
    {'id': NODE, 'name': 'Training', 'connection_status': 'unknown', 'online': False,
     'freshness': {'status': 'stale', 'sampled_at': 120, 'age_seconds': 300},
     'heartbeat_freshness': {'status': 'fresh', 'age_seconds': 1},
     'metrics_status': 'error', 'metrics_sampled_at': 120, 'metrics_error': 'sampling failed',
     'cpu_percent': 22, 'latency_ms': None, 'os_release': 'test distro'},
    {'id': OTHER, 'name': 'Other', 'online': True}],
    'transfers': [{'id': 'one', 'node_id': NODE, 'status': 'transferring', 'progress_stale': True},
                  {'id': 'two', 'node_id': OTHER, 'status': 'completed'}],
    'mcp_url': 'https://hub.test/mcp', 'version': 'test', 'update': {}}
HISTORY = {'node_id': NODE, 'metric': 'cpu', 'range': '1h', 'device': None,
           'stats': {'min': 20, 'max': 30, 'avg': 25}, 'devices': [], 'gpus': [],
           'points': [{'timestamp': 120, 'value': 25}, {'timestamp': 180, 'value': None}]}


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    (tmp_path / 'config.json').write_text(json.dumps({'admin_token': 'synthetic-test-admin'}))
    (tmp_path / 'ssh_config').write_text('')
    (tmp_path / 'inventory.json').write_text('{}')
    monkeypatch.setenv('FLEET_ROOT', str(tmp_path))
    spec = importlib.util.spec_from_file_location('fleet_test_mcp', Path(__file__).with_name('mcp_entry.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clients, requests = [], []
    real_client = REAL_HTTP_CLIENT
    response = {'value': STATE}

    async def handle(request):
        requests.append(request)
        result = response['value']
        if callable(result):
            return await result(request)
        return httpx.Response(200, json=result)

    def client_factory(**kwargs):
        client = real_client(**kwargs, transport=httpx.MockTransport(handle))
        clients.append(client)
        return client
    monkeypatch.setattr(module.httpx, 'AsyncClient', client_factory)
    shutdown = AsyncMock()
    monkeypatch.setattr(module, '_shutdown', shutdown)

    def build(**overrides):
        settings = Settings(_env_file=None, ssh_config=str(tmp_path / 'ssh_config'),
                            inventory=str(tmp_path / 'inventory.json'), audit_path=str(tmp_path / 'audit.jsonl'),
                            unsafe_notices=False, **overrides)
        return module.build_fleet_server(settings)
    return module, build, clients, requests, response, shutdown, tmp_path


async def call(server, tool_name, **args):
    result = await server.call_tool(tool_name, args)
    return ''.join(item.text for item in result.content if item.type == 'text')


@contextlib.asynccontextmanager
async def running(server):
    async with server._lowlevel_server.lifespan(server._lowlevel_server):
        yield


def test_client_is_shared_for_concurrent_tools_and_closed_on_serving_loop(fleet):
    _, build, clients, requests, response, shutdown, _ = fleet
    server = build()
    assert clients == []  # Building/discovering tools opens no HTTP client.

    async def exercise():
        entered = 0
        all_entered = asyncio.Event()
        async def handle(request):
            nonlocal entered
            entered += 1
            if entered == 3:
                all_entered.set()
            await asyncio.wait_for(all_entered.wait(), 1)
            return httpx.Response(200, json=STATE)
        response['value'] = handle
        async with running(server):
            values = await asyncio.gather(*(call(server, 'fleet_status') for _ in range(3)))
            assert all(json.loads(value) == STATE for value in values)
            assert len(clients) == 1 and not clients[0].is_closed
            assert clients[0].timeout.connect == 2 and clients[0].timeout.pool == 2
        assert clients[0].is_closed
        shutdown.assert_awaited_once_with(server.relay)
    asyncio.run(exercise())
    assert len(requests) == 3
    assert all(request.headers['authorization'] == 'Bearer synthetic-test-admin' for request in requests)


def test_http_app_enters_and_closes_same_lifespan(fleet):
    _, build, clients, _, _, shutdown, _ = fleet
    server = build(transport='http')
    app = server.streamable_http_app()
    with TestClient(app):
        assert len(clients) == 1 and not clients[0].is_closed
    assert clients[0].is_closed
    shutdown.assert_awaited_once_with(server.relay)


def test_lifespan_preserves_upstream_state_and_cleans_up_after_error(fleet, monkeypatch):
    module, build, clients, _, _, shutdown, _ = fleet
    original_build = module.build_server
    state = {'upstream': object()}
    events = []

    def build_with_lifespan(settings):
        server = original_build(settings)
        @contextlib.asynccontextmanager
        async def lifespan(_):
            events.append('enter')
            try:
                yield state
            finally:
                events.append('exit')
        server._lowlevel_server.lifespan = lifespan
        return server
    monkeypatch.setattr(module, 'build_server', build_with_lifespan)
    server = build()
    async def exercise():
        with pytest.raises(RuntimeError, match='test failure'):
            async with server._lowlevel_server.lifespan(server._lowlevel_server) as value:
                assert value is state
                raise RuntimeError('test failure')
    asyncio.run(exercise())
    assert events == ['enter', 'exit'] and clients[0].is_closed
    shutdown.assert_awaited_once()


def test_status_defaults_compatible_and_filter_is_concise_without_false_freshness(fleet):
    _, build, _, requests, _, _, tmp_path = fleet
    server = build()
    async def exercise():
        async with running(server):
            full = await call(server, 'fleet_status')
            concise = await call(server, 'fleet_status', node_id=NODE, detail=False)
        assert json.loads(full) == STATE
        data = json.loads(concise)
        assert len(data['nodes']) == 1 and len(data['transfers']) == 1
        assert 'os_release' not in data['nodes'][0]
        assert data['nodes'][0]['freshness'] == STATE['nodes'][0]['freshness']
        assert data['nodes'][0]['heartbeat_freshness'] == STATE['nodes'][0]['heartbeat_freshness']
        assert data['nodes'][0]['metrics_status'] == 'error'
        assert data['nodes'][0]['metrics_sampled_at'] == 120
        assert data['nodes'][0]['metrics_error'] == 'sampling failed'
        assert data['nodes'][0]['connection_status'] == 'unknown'
        assert data['transfers'][0]['progress_stale'] is True
        assert ': ' not in concise
    asyncio.run(exercise())
    audit = [json.loads(line) for line in (tmp_path / 'audit.jsonl').read_text().splitlines()]
    assert [row['tool'] for row in audit] == ['fleet_status', 'fleet_status']
    assert audit[-1]['args']['node_id'] == NODE and audit[-1]['tier'] == 1
    assert len(requests) == 2


def test_status_concise_preserves_network_measurements_and_unknown_rates(fleet):
    _, build, _, _, response, _, _ = fleet
    network = {
        'network_rx_bps': 2_500_000.5, 'network_tx_bps': None,
        'network_rx': 8_000_000_000, 'network_tx': 9_000_000_000,
        'network_interfaces': ['eth0'], 'network_scope': 'non_loopback',
        'network_status': 'partial', 'network_sampled_at': 180,
        'network_interval_seconds': 10.5, 'network_counter_scope': 'since_boot',
        'network_rx_errors': 1, 'network_tx_errors': 2,
        'network_rx_dropped': 3, 'network_tx_dropped': 4,
    }
    response['value'] = {**STATE, 'nodes': [{**STATE['nodes'][0], **network}, STATE['nodes'][1]]}
    server = build()

    async def exercise():
        async with running(server):
            result = json.loads(await call(server, 'fleet_status', detail=False))
        node, older_node = result['nodes']
        assert {key: node[key] for key in network} == network
        assert 'os_release' not in node
        assert not any(key in older_node for key in network)
    asyncio.run(exercise())


@pytest.mark.parametrize('node_id, expected, request_count', [
    ('../../state', 'Invalid node ID', 0),
    ('node-0000000003', 'Node not found', 1),
])
def test_status_filter_rejects_invalid_or_missing_node(fleet, node_id, expected, request_count):
    _, build, _, requests, _, _, _ = fleet
    server = build()
    async def exercise():
        async with running(server):
            assert expected in await call(server, 'fleet_status', node_id=node_id)
    asyncio.run(exercise())
    assert len(requests) == request_count


def test_monitor_returns_summary_then_curve_and_keeps_missing_latest(fleet):
    _, build, _, requests, response, _, _ = fleet
    response['value'] = HISTORY
    server = build()
    async def exercise():
        async with running(server):
            summary = json.loads(await call(server, 'fleet_monitor', node_id=NODE))
            detailed = json.loads(await call(server, 'fleet_monitor', node_id=NODE, include_points=True))
        assert 'points' not in summary and 'gpus' not in summary
        assert summary['stats'] == HISTORY['stats']
        assert summary['latest'] == {'timestamp': 180, 'value': None}
        assert summary['latest_observed'] == {'timestamp': 120, 'value': 25}
        assert summary['observed_points'] == 1 and summary['point_count'] == 2
        assert summary['sampling']['stats_scope'] == 'reported_samples'
        assert summary['unit'] == 'percent'
        assert detailed['points'] == HISTORY['points']
    asyncio.run(exercise())
    assert all(request.url.path == f'/api/nodes/{NODE}/history' for request in requests)
    assert dict(requests[0].url.params) == {'metric': 'cpu', 'range': '1h'}


def test_monitor_unknown_samples_are_not_zero_and_gpu_query_is_encoded(fleet):
    _, build, _, requests, response, _, _ = fleet
    response['value'] = {**HISTORY, 'points': [{'timestamp': 180, 'value': None}],
                         'stats': {'min': None, 'max': None, 'avg': None}}
    server = build()
    async def exercise():
        async with running(server):
            summary = json.loads(await call(server, 'fleet_monitor', node_id=NODE,
                                            metric='gpu', device=0, range='24h'))
        assert summary['latest_observed'] is None
        assert summary['stats']['avg'] is None and summary['observed_points'] == 0
    asyncio.run(exercise())
    assert dict(requests[0].url.params) == {'metric': 'gpu', 'range': '24h', 'device': '0'}


@pytest.mark.parametrize('metric', ['network_rx', 'network_tx'])
@pytest.mark.parametrize('explicit_unit', [False, True])
def test_monitor_network_rates_keep_large_values_missing_samples_and_byte_units(fleet, metric, explicit_unit):
    _, build, _, requests, response, _, _ = fleet
    history = {
        **HISTORY, 'metric': metric, 'range': '6h',
        'stats': {'min': 1_250_000, 'max': 2_500_000.5, 'avg': 1_875_000.25},
        'points': [{'timestamp': 60, 'value': 1_250_000},
                   {'timestamp': 120, 'value': 2_500_000.5},
                   {'timestamp': 180, 'value': None}],
    }
    if explicit_unit:
        history['unit'] = 'bytes_per_second'
    response['value'] = history
    server = build()

    async def exercise():
        async with running(server):
            summary = json.loads(await call(server, 'fleet_monitor', node_id=NODE,
                                            metric=metric, range='6h'))
            detailed = json.loads(await call(server, 'fleet_monitor', node_id=NODE,
                                             metric=metric, range='6h', include_points=True))
        assert summary['unit'] == detailed['unit'] == 'bytes_per_second'
        assert summary['stats'] == history['stats']
        assert summary['latest'] == {'timestamp': 180, 'value': None}
        assert summary['latest_observed'] == {'timestamp': 120, 'value': 2_500_000.5}
        assert summary['observed_points'] == 2 and summary['point_count'] == 3
        assert summary['sampling']['bucket_seconds'] == 60
        assert summary['sampling']['aggregation'] == 'minute_mean'
        assert 'points' not in summary and detailed['points'] == history['points']
    asyncio.run(exercise())
    assert all(dict(request.url.params) == {'metric': metric, 'range': '6h'} for request in requests)


@pytest.mark.parametrize('metric', ['cpu', 'memory', 'disk', 'gpu', 'network_rx', 'network_tx'])
def test_monitor_unknown_window_keeps_null_stats_and_infers_unit_for_old_responses(fleet, metric):
    module, _, _, _, _, _, _ = fleet
    history = {**HISTORY, 'metric': metric, 'points': [{'timestamp': 180, 'value': None}],
               'stats': {'min': None, 'max': None, 'avg': None}}
    result = module.monitor_result(history, include_points=True)
    assert result['unit'] == ('bytes_per_second' if metric.startswith('network_') else 'percent')
    assert result['stats'] == history['stats']
    assert result['latest_observed'] is None and result['observed_points'] == 0
    assert result['points'] == history['points']


@pytest.mark.parametrize('args', [{'node_id': '../state'}, {'node_id': NODE, 'device': 0},
                                 {'node_id': NODE, 'metric': 'gpu', 'device': 256},
                                 {'node_id': NODE, 'metric': 'network_rx', 'device': 0},
                                 {'node_id': NODE, 'metric': 'network_tx', 'device': 0}])
def test_monitor_rejects_bad_target_and_device_before_registry(fleet, args):
    _, build, _, requests, _, _, _ = fleet
    server = build()
    async def exercise():
        async with running(server):
            result = await call(server, 'fleet_monitor', **args)
            assert 'ValueError' in result
    asyncio.run(exercise())
    assert requests == []


def test_guarded_policy_still_blocks_changes_and_audits_monitor(fleet):
    _, build, _, requests, response, _, tmp_path = fleet
    response['value'] = HISTORY
    server = build(policy_mode='guarded')
    async def exercise():
        async with running(server):
            assert 'stats' in json.loads(await call(server, 'fleet_monitor', node_id=NODE))
            assert 'DENIED' in await call(server, 'fleet_enroll', name='test')
            assert 'DENIED' in await call(server, 'fleet_revoke', node_id=NODE)
    asyncio.run(exercise())
    assert len(requests) == 1
    audit = [json.loads(line) for line in (tmp_path / 'audit.jsonl').read_text().splitlines()]
    assert [row['tier'] for row in audit] == [1, 2, 2]
    assert [row['denied'] for row in audit] == [False, True, True]


@pytest.mark.parametrize('failure, expected', [
    (httpx.ConnectError('private transport details'), 'registry is unavailable'),
    (httpx.ReadTimeout('private timeout details'), 'request timed out'),
    (httpx.PoolTimeout('private pool details'), 'registry is busy'),
    (httpx.Response(503, text='private server details'), 'HTTP 503'),
    (httpx.Response(200, text='private non-json details'), 'invalid JSON'),
])
def test_registry_errors_are_safe_and_mutations_are_never_retried(fleet, failure, expected):
    _, build, _, requests, response, _, _ = fleet
    server = build()
    async def handle(_):
        if isinstance(failure, Exception):
            raise failure
        return failure
    response['value'] = handle
    async def exercise():
        async with running(server):
            result = await call(server, 'fleet_enroll', name='test')
        assert expected in result and 'private' not in result and 'synthetic-test-admin' not in result
    asyncio.run(exercise())
    assert len(requests) == 1


def test_total_deadline_and_cancellation_are_bounded_and_audited(fleet, monkeypatch):
    module, build, _, requests, response, _, tmp_path = fleet
    monkeypatch.setattr(module, 'REGISTRY_TIMEOUT', .02)
    server = build()
    async def handle(_):
        await asyncio.sleep(10)
        return httpx.Response(200, json=STATE)
    response['value'] = handle
    async def exercise():
        async with running(server):
            result = await asyncio.wait_for(call(server, 'fleet_status'), .5)
            assert 'request timed out' in result
            monkeypatch.setattr(module, 'REGISTRY_TIMEOUT', 20)
            task = asyncio.create_task(call(server, 'fleet_status'))
            while len(requests) < 2:
                await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(exercise())
    audit = [json.loads(line) for line in (tmp_path / 'audit.jsonl').read_text().splitlines()]
    assert audit[-1]['action'] == 'cancelled'


def test_monitor_obeys_relay_output_cap(fleet):
    _, build, _, _, response, _, _ = fleet
    response['value'] = {**HISTORY, 'points': [{'timestamp': i * 60, 'value': 50} for i in range(1440)]}
    server = build(max_output=1024)
    async def exercise():
        async with running(server):
            summary = await call(server, 'fleet_monitor', node_id=NODE)
            curve = await call(server, 'fleet_monitor', node_id=NODE, include_points=True)
        assert len(summary) < 1024 and 'TRUNCATED' in curve
        assert len(curve) < 1200
    asyncio.run(exercise())


def test_new_tool_schema_preserves_defaults_and_supports_discovery_without_network(fleet):
    _, build, clients, requests, _, _, _ = fleet
    server = build()
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    status = tools['fleet_status'].input_schema
    monitor = tools['fleet_monitor'].input_schema
    assert status['properties']['detail']['default'] is True
    assert status['properties']['node_id']['default'] == ''
    assert monitor['required'] == ['node_id']
    assert monitor['properties']['metric']['enum'] == ['cpu', 'memory', 'disk', 'gpu',
                                                     'network_rx', 'network_tx']
    assert monitor['properties']['range']['enum'] == ['1h', '6h', '24h']
    assert monitor['properties']['include_points']['default'] is False
    assert clients == [] and requests == []


@pytest.mark.parametrize('settings', [{'policy_mode': 'readonly'}, {'policy_deny': 'fleet_monitor'}])
def test_monitor_cannot_bypass_existing_readonly_or_deny_rules(fleet, settings):
    _, build, _, requests, _, _, tmp_path = fleet
    server = build(**settings)
    async def exercise():
        async with running(server):
            assert 'DENIED' in await call(server, 'fleet_monitor', node_id=NODE)
    asyncio.run(exercise())
    assert requests == []
    audit = json.loads((tmp_path / 'audit.jsonl').read_text())
    assert audit['tool'] == 'fleet_monitor' and audit['denied'] is True


def test_loopback_registry_reuses_one_tcp_connection(fleet, monkeypatch):
    module, build, _, _, _, shutdown, _ = fleet
    accepted = 0
    clients = []
    writers = set()

    async def handle(reader, writer):
        nonlocal accepted
        accepted += 1
        writers.add(writer)
        try:
            while True:
                headers = await reader.readuntil(b'\r\n\r\n')
                content_length = next((int(line.split(b':', 1)[1])
                                       for line in headers.split(b'\r\n')
                                       if line.lower().startswith(b'content-length:')), 0)
                await reader.readexactly(content_length)
                body = json.dumps(STATE).encode()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '
                             + str(len(body)).encode() + b'\r\n\r\n' + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writers.discard(writer)
            writer.close()
            await writer.wait_closed()

    async def exercise():
        listener = await asyncio.start_server(handle, '127.0.0.1', 0)
        port = listener.sockets[0].getsockname()[1]
        def factory(**kwargs):
            kwargs['base_url'] = f'http://127.0.0.1:{port}'
            client = REAL_HTTP_CLIENT(**kwargs)
            clients.append(client)
            return client
        monkeypatch.setattr(module.httpx, 'AsyncClient', factory)
        server = build()
        try:
            async with listener, running(server):
                for _ in range(12):
                    assert json.loads(await call(server, 'fleet_status')) == STATE
            assert accepted == 1 and len(clients) == 1 and clients[0].is_closed
            shutdown.assert_awaited_once_with(server.relay)
        finally:
            for writer in tuple(writers):
                writer.close()
                await writer.wait_closed()
    asyncio.run(exercise())
