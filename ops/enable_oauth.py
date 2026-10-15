#!/usr/bin/env python3
"""Migrate an existing Fleet Caddy gateway to the installed OAuth verifier."""
import datetime
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.request


def main():
    root = Path(os.environ.get('FLEET_ROOT', '/var/lib/fleet'))
    config = json.loads((root / 'config.json').read_text())
    caddyfile = root / 'Caddyfile'
    original = caddyfile.read_text()
    old = ('        @unauthorized not header Authorization "Bearer ' + config['admin_token'] + '"\n'
           '        respond @unauthorized "Authentication required" 401')
    new = ('        forward_auth 127.0.0.1:8090 {\n'
           '            uri /internal/mcp-auth\n'
           '        }')
    if new in original:
        print('Fleet OAuth gateway is already enabled')
        return
    if original.count(old) != 1:
        raise SystemExit('Existing Caddy authentication block differs; inspect before migration')
    verifier = 'http://127.0.0.1:8090/internal/mcp-auth'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(verifier, headers={
            'Authorization': 'Bearer ' + config['admin_token']}), timeout=10) as response:
        assert 200 <= response.status < 300
    try:
        opener.open(verifier, timeout=10)
    except urllib.error.HTTPError as error:
        assert error.code == 401 and 'resource_metadata=' in error.headers.get('WWW-Authenticate', '')
    else:
        raise SystemExit('OAuth verifier must reject unauthenticated requests')
    with opener.open('http://127.0.0.1:8090/.well-known/oauth-authorization-server', timeout=10) as response:
        metadata = json.load(response)
        assert metadata['registration_endpoint'] and 'S256' in metadata['code_challenge_methods_supported']
    staged = caddyfile.with_name('Caddyfile.oauth-next')
    updated = original.replace(old, new)
    staged.write_text(updated)
    staged.chmod(0o600)
    binary = '/opt/fleet/bin/caddy'
    subprocess.run([binary, 'validate', '--config', str(staged), '--adapter', 'caddyfile'], check=True)
    backup = caddyfile.with_name('Caddyfile.before-oauth-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.write_bytes(caddyfile.read_bytes())
    backup.chmod(0o600)
    try:
        # Preserve ownership and mode of the file read by the existing service.
        caddyfile.write_text(updated)
        subprocess.run([binary, 'reload', '--config', str(caddyfile), '--adapter', 'caddyfile'], check=True)
    except BaseException:
        caddyfile.write_text(original)
        subprocess.run([binary, 'reload', '--config', str(caddyfile), '--adapter', 'caddyfile'], check=False)
        raise
    finally:
        staged.unlink(missing_ok=True)
    print('Fleet OAuth gateway enabled; legacy Bearer remains supported')


if __name__ == '__main__':
    main()
