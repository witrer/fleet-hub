"""Relay Shell with authenticated Fleet registry management tools."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from pathlib import Path

import httpx
from mcp.server.mcpserver import Context
from relay_shell.__main__ import _shutdown
from relay_shell.config import get_settings
from relay_shell.policy import Tier
from relay_shell.server import build_server, http_transport_kwargs


def build_fleet_server(settings=None):
    settings = settings or get_settings()
    root = Path(os.environ.get('FLEET_ROOT', '/var/lib/fleet'))
    config = json.loads((root / 'config.json').read_text())
    token = config['admin_token']
    if not isinstance(token, str) or not token:
        raise ValueError('Fleet admin credential is missing')
    server = build_server(settings)
    relay = server.relay

    async def api(method, path, payload=None):
        try:
            async with httpx.AsyncClient(
                base_url='http://127.0.0.1:8090', timeout=20, trust_env=False,
                headers={'Authorization': 'Bearer ' + token},
            ) as client:
                response = await client.request(method, path, json=payload)
        except httpx.HTTPError:
            raise RuntimeError('Fleet registry is unavailable') from None
        if response.is_error:
            raise RuntimeError(f'Fleet registry returned HTTP {response.status_code}')
        return json.dumps(response.json(), ensure_ascii=False)

    async def audited(tool, method, path, payload, ctx, tier):
        async def work():
            if tool == 'fleet_revoke' and not re.fullmatch(r'node-[0-9a-f]{10}', payload['node_id']):
                raise ValueError('Invalid node ID; use an ID returned by fleet_status')
            return await api(method, path, payload), None
        return await relay.run(
            tool=tool, ctx=ctx, audit_args=payload or {},
            policy_text=json.dumps(payload or {}, ensure_ascii=False),
            max_output=relay.clamp_output(settings.max_output),
            work=work, min_tier=tier, json_notices=True,
        )

    @server.tool()
    async def fleet_status(ctx: Context | None = None) -> str:
        """List registered servers with current heartbeat/tunnel online state and file transfer progress.

        Node IDs can be passed to the existing ssh_exec, ssh_spawn, and SFTP tools.
        """
        return await audited('fleet_status', 'GET', '/api/state', None, ctx, Tier.READ_ONLY)

    @server.tool()
    async def fleet_enroll(name: str, ctx: Context | None = None) -> str:
        """Generate a one-use enrollment command for a Linux server, valid for 30 minutes.

        Return the command to the owner to execute on the intended training server.
        """
        return await audited('fleet_enroll', 'POST', '/api/enrollments',
                             {'name': name}, ctx, Tier.STATEFUL)

    @server.tool()
    async def fleet_revoke(node_id: str, ctx: Context | None = None) -> str:
        """Revoke a registered node's tunnel credentials and remove it from the active inventory."""
        return await audited('fleet_revoke', 'POST', f'/api/nodes/{node_id}/revoke',
                             {'node_id': node_id}, ctx, Tier.STATEFUL)

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
    finally:
        with contextlib.suppress(Exception):
            asyncio.run(_shutdown(server.relay))


if __name__ == '__main__':
    main()
