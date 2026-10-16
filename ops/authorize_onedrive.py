#!/usr/bin/env python3
"""Explicit, one-time personal OneDrive authorization; never prints bearer tokens."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


AUTH = 'https://login.microsoftonline.com/consumers/oauth2/v2.0'
GRAPH = 'https://graph.microsoft.com/v1.0'
SCOPE = 'offline_access Files.ReadWrite'
TOKEN_FILE = 'onedrive-token.json'
MAX_RESPONSE = 1024 * 1024


class AuthorizationError(Exception):
    """A safe, fixed error suitable for display without remote response bodies."""


class NetworkError(AuthorizationError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class HTTPJSON:
    def __init__(self, opener=None):
        # OAuth and Graph metadata endpoints do not need redirects. In particular,
        # never forward a bearer token or device code to a redirect destination.
        self.opener = opener or urllib.request.build_opener(NoRedirect())

    def request(self, method, url, *, form=None, body=None, token=None, timeout=20):
        headers = {'Accept': 'application/json'}
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        elif body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        if token:
            headers['Authorization'] = 'Bearer ' + token
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            try:
                response = self.opener.open(request, timeout=max(0.1, timeout))
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status = response.code
                raw = response.read(MAX_RESPONSE + 1)
        except (OSError, urllib.error.URLError):
            raise NetworkError('Network request failed; check connectivity and retry.') from None
        if len(raw) > MAX_RESPONSE:
            raise AuthorizationError('Microsoft returned an oversized response.')
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            # Never print a response body: it may contain credentials.
            raise AuthorizationError('Microsoft returned an invalid JSON response.') from None
        if not isinstance(value, dict):
            raise AuthorizationError('Microsoft returned an invalid JSON response.')
        return status, value


def positive_number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 86400:
        raise AuthorizationError('Microsoft returned an invalid ' + label + '.')
    return float(value)


def authorize(client_id, *, http, timeout=900, clock=time.monotonic,
              sleep=time.sleep, output=print):
    """Device-code polling is bounded by both local and server expiry deadlines."""
    started = clock()
    status, device = http.request('POST', AUTH + '/devicecode', form={
        'client_id': client_id, 'scope': SCOPE,
    }, timeout=min(20, timeout))
    if status != 200:
        raise AuthorizationError('Could not start device authorization; check the application registration.')
    expires = positive_number(device.get('expires_in'), 'device-code lifetime')
    interval = max(1, positive_number(device.get('interval', 5), 'polling interval'))
    code, uri, user_code = (device.get(key) for key in ('device_code', 'verification_uri', 'user_code'))
    try:
        parsed = urllib.parse.urlsplit(uri) if isinstance(uri, str) else None
    except ValueError:
        parsed = None
    if (not isinstance(code, str) or not code or not parsed or parsed.scheme != 'https'
            or parsed.hostname not in {'microsoft.com', 'www.microsoft.com', 'login.microsoftonline.com', 'aka.ms'}
            or parsed.username or parsed.password or parsed.fragment
            or not isinstance(user_code, str) or not re.fullmatch(r'[A-Za-z0-9-]{4,32}', user_code)):
        raise AuthorizationError('Microsoft returned invalid device authorization instructions.')
    output('Open ' + uri + ' and enter code: ' + user_code)
    output('Sign in with the personal OneDrive account used for Fleet transfers.')
    deadline = min(started + timeout, started + expires)
    while clock() < deadline:
        sleep(min(interval, max(0, deadline - clock())))
        remaining = deadline - clock()
        if remaining <= 0:
            break
        try:
            status, token = http.request('POST', AUTH + '/token', form={
                'client_id': client_id,
                'grant_type': 'urn:ietf:params:oauth:grant-type:device_code',
                'device_code': code,
            }, timeout=min(20, remaining))
        except NetworkError:
            interval = min(60, interval + 5)
            continue
        if status == 200:
            if (not isinstance(token.get('refresh_token'), str) or not token['refresh_token']
                    or not isinstance(token.get('access_token'), str) or not token['access_token']):
                raise AuthorizationError('Authorization did not return both required tokens; check offline_access consent.')
            return token
        error = token.get('error')
        if error == 'authorization_pending':
            continue
        if error == 'slow_down' or status == 429:
            interval = min(60, interval + 5)
            continue
        if status >= 500:
            interval = min(60, interval + 5)
            continue
        if error in {'authorization_declined', 'access_denied'}:
            raise AuthorizationError('Authorization was declined; no local token was changed.')
        if error in {'expired_token', 'bad_verification_code'}:
            raise AuthorizationError('The device code expired or is invalid; run authorization again.')
        raise AuthorizationError('Authorization failed; check personal-account support and public client flows.')
    raise AuthorizationError('Authorization timed out; run it again when ready to sign in.')


def transfer_folder(http, access_token, folder):
    status, drive = http.request('GET', GRAPH + '/me/drive?$select=id,driveType,quota', token=access_token)
    if status != 200 or not isinstance(drive.get('id'), str) or not drive['id']:
        raise AuthorizationError('Could not read the personal OneDrive; ensure the account has an active drive.')
    if drive.get('driveType') != 'personal':
        raise AuthorizationError('This setup command supports personal OneDrive accounts only.')
    drive_url = GRAPH + '/drives/' + urllib.parse.quote(drive['id'], safe='')
    folder_url = drive_url + '/root:/' + urllib.parse.quote(folder, safe='') + '?$select=id,folder'
    status, item = http.request('GET', folder_url, token=access_token)
    if status == 404:
        status, item = http.request('POST', drive_url + '/root/children', token=access_token, body={
            'name': folder, 'folder': {}, '@microsoft.graph.conflictBehavior': 'fail',
        })
        if status == 409:
            # A parallel setup might have created the same folder; never rename it silently.
            status, item = http.request('GET', folder_url, token=access_token)
    if (status not in (200, 201) or not isinstance(item.get('id'), str) or not item['id']
            or not isinstance(item.get('folder'), dict)):
        raise AuthorizationError('Could not find or create the transfer folder; check its name and Files.ReadWrite consent.')
    return drive['id'], item['id'], drive.get('quota', {})


def atomic_token(root, refresh_token):
    root = Path(root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix='.onedrive-token-', dir=root)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump({'refresh_token': refresh_token}, stream)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / TOKEN_FILE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def client_id_argument(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError('client-id must be your application registration UUID') from None


def folder_argument(value):
    if (not value or value in {'.', '..'} or value != value.strip() or len(value) > 200
            or value.endswith('.') or any(c in value for c in '/\\"*:<>?|')
            or any(ord(c) < 32 for c in value)):
        raise argparse.ArgumentTypeError('folder must be one valid OneDrive folder name, without a path')
    return value


def main(argv=None, *, http=None, clock=time.monotonic, sleep=time.sleep, output=print):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(os.environ.get('FLEET_ROOT', '/var/lib/fleet')))
    parser.add_argument('--client-id', required=True, type=client_id_argument)
    parser.add_argument('--folder', default='FleetHubTransfers', type=folder_argument)
    parser.add_argument('--timeout', type=int, default=900, help='maximum authorization wait in seconds (1–3600)')
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 3600:
        parser.error('--timeout must be between 1 and 3600 seconds')
    try:
        http = http or HTTPJSON()
        token = authorize(args.client_id, http=http, timeout=args.timeout, clock=clock, sleep=sleep, output=output)
        drive_id, folder_id, quota = transfer_folder(http, token['access_token'], args.folder)
        atomic_token(args.root, token['refresh_token'])
    except AuthorizationError as error:
        output('OneDrive setup failed: ' + str(error))
        return 1
    except OSError:
        output('OneDrive setup failed: could not save the token file; check the Fleet service user and root directory permissions.')
        return 1
    except KeyboardInterrupt:
        output('OneDrive authorization cancelled.')
        return 130
    output('Authorization saved to onedrive-token.json with mode 0600; no config file was changed.')
    if isinstance(quota, dict) and isinstance(quota.get('remaining'), int) and quota['remaining'] >= 0:
        output('OneDrive reports ' + str(quota['remaining']) + ' remaining bytes; allow room for concurrent transfers.')
    output('Merge this object into the existing Fleet config.json using the same service user:')
    output(json.dumps({'onedrive': {'client_id': args.client_id, 'tenant': 'consumers',
                                  'drive_id': drive_id, 'folder_id': folder_id}}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
