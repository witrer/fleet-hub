"""Relay Shell with authenticated Fleet registry management tools."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Literal

import httpx
from mcp.server.mcpserver import Context
from relay_shell.config import get_settings
from relay_shell.__main__ import _shutdown
from relay_shell.policy import Tier
from relay_shell.server import build_server, http_transport_kwargs

REGISTRY_TIMEOUT = 20
NODE_ID = re.compile(r'node-[0-9a-f]{10}')
STATUS_FIELDS = (
    'id', 'name', 'ssh_user', 'online', 'connection_status', 'frp_status', 'last_seen',
    'agent_version', 'agent_update', 'relay_protocol',
    'freshness', 'metrics_status', 'metrics_sampled_at', 'metrics_error', 'heartbeat_freshness',
    'cpu_percent', 'memory_percent', 'memory_used_bytes', 'memory_total_bytes',
    'disk_percent', 'disk_used_bytes', 'disk_total_bytes', 'gpu_status', 'gpu_percent',
    'gpus', 'latency_ms', 'latency_success', 'latency_sample_at',
    'network_rx_bps', 'network_tx_bps', 'network_rx', 'network_tx',
    'network_interfaces', 'network_scope', 'network_status', 'network_sampled_at',
    'network_interval_seconds', 'network_counter_scope', 'network_rx_errors',
    'network_tx_errors', 'network_rx_dropped', 'network_tx_dropped',
)


def validate_node_id(node_id):
    if not NODE_ID.fullmatch(node_id):
        raise ValueError('Invalid node ID; use an ID returned by fleet_status')


def status_result(data, node_id, detail):
    if node_id:
        nodes = [node for node in data['nodes'] if node['id'] == node_id]
        if not nodes:
            raise ValueError('Node not found in the active inventory')
        data = {**data, 'nodes': nodes,
                'transfers': [item for item in data['transfers'] if item['node_id'] == node_id]}
    if not detail:
        data = {**data, 'nodes': [{key: node[key] for key in STATUS_FIELDS if key in node}
                                for node in data['nodes']]}
    return data


def monitor_result(data, include_points):
    points = data.get('points', [])
    result = {key: data.get(key) for key in ('node_id', 'metric', 'range', 'device', 'stats', 'devices')}
    result['unit'] = data.get('unit') or (
        'bytes_per_second' if data.get('metric') in ('network_rx', 'network_tx') else 'percent')
    result.update(
        sampling={'bucket_seconds': 60, 'aggregation': 'minute_mean',
                  'missing_values': 'null', 'stats_scope': 'reported_samples'},
        point_count=len(points),
        observed_points=sum(point['value'] is not None for point in points),
        latest=points[-1] if points else None,
        latest_observed=next((point for point in reversed(points) if point['value'] is not None), None),
    )
    if include_points:
        result['points'] = points
    return result


def build_fleet_server(settings=None):
    settings = settings or get_settings()
    root = Path(os.environ.get('FLEET_ROOT', '/var/lib/fleet'))
    config = json.loads((root / 'config.json').read_text())
    token = config['admin_token']
    if not isinstance(token, str) or not token:
        raise ValueError('Fleet admin credential is missing')
    server = build_server(settings)
    relay = server.relay
    client = None

    # Relay's factory owns construction. Wrap the SDK's existing lifespan so
    # HTTP and stdio share one bounded pool and close it on the serving loop.
    # The SDK enters this once for the HTTP manager, not per request.
    lowlevel = server._lowlevel_server
    original_lifespan = lowlevel.lifespan

    @contextlib.asynccontextmanager
    async def lifespan(app):
        nonlocal client
        async with original_lifespan(app) as state:
            async with httpx.AsyncClient(
                base_url='http://127.0.0.1:8090', trust_env=False,
                timeout=httpx.Timeout(REGISTRY_TIMEOUT, connect=2, pool=2),
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=4,
                                    keepalive_expiry=30),
                headers={'Authorization': 'Bearer ' + token},
            ) as registry_client:
                client = registry_client
                try:
                    yield state
                finally:
                    client = None
                    await _shutdown(relay)

    lowlevel.lifespan = lifespan

    async def api(method, path, payload=None, params=None):
        if client is None:
            raise RuntimeError('Fleet registry client is not running')
        try:
            # HTTPX bounds individual phases; this also bounds total wall time.
            async with asyncio.timeout(REGISTRY_TIMEOUT):
                response = await client.request(method, path, json=payload, params=params)
        except httpx.PoolTimeout:
            raise RuntimeError('Fleet registry is busy; retry the read later') from None
        except (httpx.TimeoutException, TimeoutError):
            raise RuntimeError('Fleet registry request timed out; check status before retrying a change') from None
        except httpx.HTTPError:
            raise RuntimeError('Fleet registry is unavailable') from None
        if response.is_error:
            raise RuntimeError(f'Fleet registry returned HTTP {response.status_code}')
        try:
            return response.json()
        except ValueError:
            raise RuntimeError('Fleet registry returned invalid JSON') from None

    async def audited(tool, args, ctx, tier, work):
        async def run():
            return json.dumps(await work(), ensure_ascii=False, separators=(',', ':')), None
        return await relay.run(
            tool=tool, ctx=ctx, audit_args=args,
            policy_text=json.dumps(args, ensure_ascii=False),
            max_output=relay.clamp_output(settings.max_output),
            work=run, min_tier=tier, json_notices=True,
        )

    @server.tool()
    async def fleet_status(node_id: str = '', detail: bool = True, ctx: Context | None = None) -> str:
        """List node heartbeat/tunnel state, metrics and recent transfer progress.

        Prefer detail=false for a concise operational overview. Pass node_id to
        focus on one node and its transfers; detail=true preserves all fields.
        Freshness and connection_status distinguish stale or unknown observations.
        Node IDs work with ssh_exec, ssh_spawn and SFTP. For one command across
        nodes use ssh_fanout; for historical CPU/memory/disk/GPU or network
        receive/transmit rates use fleet_monitor.
        """
        async def work():
            if node_id:
                validate_node_id(node_id)
            return status_result(await api('GET', '/api/state'), node_id, detail)
        return await audited('fleet_status', {'node_id': node_id, 'detail': detail},
                             ctx, Tier.READ_ONLY, work)

    @server.tool()
    async def fleet_monitor(node_id: str,
                            metric: Literal['cpu', 'memory', 'disk', 'gpu', 'network_rx', 'network_tx'] = 'cpu',
                            range: Literal['1h', '6h', '24h'] = '1h', device: int | None = None,
                            include_points: bool = False, ctx: Context | None = None) -> str:
        """Read stored node monitoring without opening SSH or running shell commands.

        Returns window stats, the latest minute (null when missing), and the last
        observed minute with its timestamp. unit is percent for CPU/memory/disk/GPU
        and bytes_per_second for network_rx/network_tx receive/transmit rates.
        Missing is unknown, not zero. device selects GPU index 0..255 with metric=gpu.
        Set include_points=true only when the full minute curve is needed; the
        normal output cap still applies. Heartbeat RTT is in fleet_status.
        """
        args = {'node_id': node_id, 'metric': metric, 'range': range,
                'device': device, 'include_points': include_points}

        async def work():
            validate_node_id(node_id)
            if (metric not in ('cpu', 'memory', 'disk', 'gpu', 'network_rx', 'network_tx')
                    or range not in ('1h', '6h', '24h')):
                raise ValueError('Unsupported metric or range')
            if device is not None and (metric != 'gpu' or isinstance(device, bool) or not 0 <= device <= 255):
                raise ValueError('GPU device must be an index from 0 to 255')
            params = {'metric': metric, 'range': range}
            if device is not None:
                params['device'] = device
            data = await api('GET', f'/api/nodes/{node_id}/history', params=params)
            return monitor_result(data, include_points)
        return await audited('fleet_monitor', args, ctx, Tier.READ_ONLY, work)

    @server.tool()
    async def fleet_enroll(name: str, ctx: Context | None = None) -> str:
        """Generate a one-use enrollment command for a Linux server, valid for 30 minutes.

        Return the command to the owner to execute on the intended training server.
        """
        args = {'name': name}
        return await audited('fleet_enroll', args, ctx, Tier.STATEFUL,
                             lambda: api('POST', '/api/enrollments', args))

    @server.tool()
    async def fleet_revoke(node_id: str, ctx: Context | None = None) -> str:
        """Revoke a registered node's tunnel credentials and remove it from the active inventory."""
        async def work():
            validate_node_id(node_id)
            return await api('POST', f'/api/nodes/{node_id}/revoke', {'node_id': node_id})
        return await audited('fleet_revoke', {'node_id': node_id}, ctx, Tier.STATEFUL, work)

    return server


def main():
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    server = build_fleet_server(settings)
    try:
        if settings.transport == 'http':
            server.run(transport='streamable-http', **http_transport_kwargs(settings))
        else:
            server.run(transport='stdio')
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
