"""Fleet owner consent around Relay Shell's persisted OAuth implementation."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from fastapi import Request
from mcp.server.auth.handlers.metadata import MetadataHandler
from mcp.server.auth.provider import AuthorizeError, TokenError, construct_redirect_uri
from mcp.server.auth.routes import (
    build_metadata,
    cors_middleware,
    create_auth_routes,
    create_protected_resource_routes,
)
from relay_shell.auth.oauth import FileOAuthProvider, build_auth_settings
from relay_shell.auth.resource import TokenResourceMiddleware
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

COOKIE = 'fleet_oauth_csrf'
CONSENT_TTL = 600
SCOPES = ['mcp:tools']


class PublicClientRevocationCompatibility:
    """Supply the optional field incorrectly required by MCP SDK 2.3's revoke model."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        content_type = dict(scope.get('headers', [])).get(b'content-type', b'').split(b';', 1)[0].strip().lower()
        if scope.get('method') != 'POST' or content_type != b'application/x-www-form-urlencoded':
            await self.app(scope, receive, send)
            return
        chunks, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            chunk = message.get('body', b'')
            size += len(chunk)
            if size > 65536:
                await JSONResponse({'error': 'invalid_request'}, status_code=413)(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get('more_body', False):
                break
        body = b''.join(chunks)
        try:
            names = {key for key, _ in parse_qsl(body.decode('utf-8'), keep_blank_values=True, max_num_fields=64)}
        except (UnicodeDecodeError, ValueError):
            names = {'client_secret'}  # Leave malformed input to the SDK's validation.
        if 'client_secret' not in names:
            body += (b'&' if body else b'') + b'client_secret='
        scope = {**scope, 'headers': [(key, value) for key, value in scope.get('headers', [])
                                     if key != b'content-length'] + [(b'content-length', str(len(body)).encode())]}
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {'type': 'http.request', 'body': body, 'more_body': False}
            return await receive()

        await self.app(scope, replay, send)


class FleetOAuthProvider(FileOAuthProvider):
    """Protocol validation stays in the SDK; every new grant requires owner consent."""

    def __init__(self, base, root):
        super().__init__(str(Path(root) / 'oauth'), single_client=False,
                         access_ttl=3600, refresh_ttl=2592000, code_ttl=300,
                         resource_url=base + '/mcp', require_approval=False)
        self.base = base
        self.pending = {}

    def live_pending(self, ticket):
        now = time.time()
        for key in list(self.pending):
            if self.pending[key]['expires_at'] <= now:
                del self.pending[key]
        return self.pending.get(ticket)

    async def authorize(self, client, params):
        # The SDK checks registered callback, scope and S256 before reaching this hook.
        try:
            self._checked_resource(self._resource if params.resource is None else params.resource)
        except (ValueError, TokenError) as exc:
            raise AuthorizeError(error='invalid_target', error_description='Resource is not this server') from exc
        if not set(params.scopes or SCOPES).issubset(SCOPES):
            raise AuthorizeError(error='invalid_scope', error_description='Unknown Fleet scope')
        self.live_pending('')
        if len(self.pending) >= 256:
            raise AuthorizeError(error='temporarily_unavailable', error_description='Retry authorization shortly')
        ticket = secrets.token_urlsafe(32)
        self.pending[ticket] = {'client': client, 'params': params, 'expires_at': time.time() + CONSENT_TTL,
                                'csrf_hash': None}
        return self.base + '/oauth/consent?ticket=' + ticket

    async def complete(self, ticket):
        pending = self.live_pending(ticket)
        if pending is None:
            return None
        # Consume the request before awaiting so a second form submission cannot mint another code.
        self.pending.pop(ticket)
        return await super().authorize(pending['client'], pending['params'])


def install_oauth(app, cfg, root):
    base = cfg.get('oauth_base_url', cfg['base_url']).rstrip('/')
    owner_token = cfg['admin_token']
    if not isinstance(owner_token, str) or not owner_token:
        raise ValueError('Fleet owner credential is required')
    origin_parts = urlsplit(base)
    expected_origin = origin_parts.scheme + '://' + origin_parts.netloc
    provider = FleetOAuthProvider(base, root)
    app.state.oauth_provider = provider
    auth = build_auth_settings(base, base + '/mcp')
    metadata_url = base + '/.well-known/oauth-protected-resource/mcp'
    challenge = 'Bearer resource_metadata="' + metadata_url + '", scope="mcp:tools"'
    # no-referrer makes a browser's navigation POST send Origin: null. Keep the
    # same-origin form identifiable while withholding its URL from the callback.
    headers = {'Cache-Control': 'no-store', 'Pragma': 'no-cache', 'Referrer-Policy': 'same-origin',
               'X-Frame-Options': 'DENY',
               'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"}

    @app.middleware('http')
    async def discovery_diagnostics(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith('/.well-known/') or request.url.path == '/register':
            # Record discovery compatibility failures without query strings, credentials or bodies.
            logging.getLogger('uvicorn.error').info('oauth-discovery %s', json.dumps({
                'method': request.method, 'path': request.url.path, 'status': response.status_code,
                'user_agent': request.headers.get('user-agent', '')[:160]}))
        return response

    def reject(message, status=400):
        return JSONResponse({'error': message}, status_code=status, headers=headers)

    @app.api_route('/internal/mcp-auth', methods=['GET', 'POST', 'HEAD'])
    async def mcp_auth(request: Request):
        scheme, _, token = request.headers.get('authorization', '').partition(' ')
        if scheme.lower() != 'bearer' or not token:
            return Response(status_code=401, headers={'WWW-Authenticate': challenge, 'Cache-Control': 'no-store'})
        if hmac.compare_digest(token.encode(), owner_token.encode()):
            return Response(status_code=204)
        access = await provider.load_access_token(token)
        if access is None or access.resource != base + '/mcp' or not access.expires_at or access.expires_at <= time.time():
            return Response(status_code=401, headers={'WWW-Authenticate': challenge, 'Cache-Control': 'no-store'})
        if not set(SCOPES).issubset(access.scopes):
            return Response(status_code=403, headers={'WWW-Authenticate': challenge + ', error="insufficient_scope"',
                                                     'Cache-Control': 'no-store'})
        return Response(status_code=204)

    @app.get('/oauth/consent')
    async def consent_page(request: Request):
        ticket = request.query_params.get('ticket', '')
        pending = provider.live_pending(ticket)
        if pending is None:
            return reject('授权请求已过期，请在客户端重新连接。')
        csrf = secrets.token_urlsafe(32)
        pending['csrf_hash'] = hashlib.sha256(csrf.encode()).hexdigest()
        client = pending['client']
        client_name = html.escape(getattr(client, 'client_name', None) or client.client_id or 'MCP 客户端')
        callback = html.escape(str(pending['params'].redirect_uri))
        page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>连接 Fleet</title>
<style>body{font:16px system-ui;background:#f3f5f8;color:#162233;margin:0;padding:32px 16px}
main{max-width:480px;margin:5vh auto;background:white;padding:30px;border-radius:16px;box-shadow:0 8px 40px #152a4612}
h1{font-size:26px;margin-top:0}p{line-height:1.65}small{display:block;overflow-wrap:anywhere;color:#617084}
label{display:block;margin:24px 0 8px}input{box-sizing:border-box;width:100%;padding:12px;border:1px solid #b8c4d1;border-radius:8px}
.buttons{display:flex;gap:10px;margin-top:22px}button{padding:12px 20px;border:0;border-radius:8px;cursor:pointer}
.approve{background:#1769e0;color:white} .help{font-size:14px;color:#617084}</style><main>
<h1>授权连接 Fleet</h1><p><strong>''' + client_name + '''</strong> 请求访问你的服务器。</p>
<p>授权后，该客户端可查看服务器、执行 SSH 命令、管理终端和传输文件。</p>
<small>回调地址：''' + callback + '''</small>
<form method="post" action="/oauth/consent">
<input type="hidden" name="ticket" value="''' + html.escape(ticket, quote=True) + '''">
<input type="hidden" name="csrf" value="''' + csrf + '''">
<label for="admin_token">Fleet 面板访问令牌</label>
<input id="admin_token" name="admin_token" type="password" autocomplete="off" placeholder="输入面板使用的访问令牌">
<p class="help">令牌只用于本次身份验证，不会交给客户端。</p>
<div class="buttons"><button class="approve" name="decision" value="approve">授权连接</button>
<button name="decision" value="deny">取消</button></div></form></main></html>'''
        response = HTMLResponse(page, headers=headers)
        response.set_cookie(COOKIE, csrf, max_age=CONSENT_TTL, httponly=True,
                            secure=origin_parts.scheme == 'https', samesite='lax', path='/oauth/consent')
        return response

    @app.post('/oauth/consent')
    async def consent_submit(request: Request):
        if request.headers.get('origin') != expected_origin:
            return reject('授权页面来源不匹配。', 403)
        if len(await request.body()) > 16384:
            return reject('授权表单过大。', 413)
        async with request.form(max_files=0, max_fields=8) as form:
            fields = {key: form.get(key, '') for key in ('ticket', 'csrf', 'admin_token', 'decision')}
        if not all(isinstance(value, str) for value in fields.values()):
            return reject('授权表单无效。')
        ticket, csrf = fields['ticket'], fields['csrf']
        pending = provider.live_pending(ticket)
        cookie = request.cookies.get(COOKIE, '')
        if (pending is None or not csrf or not cookie or not pending['csrf_hash'] or
                not hmac.compare_digest(csrf.encode(), cookie.encode()) or
                not hmac.compare_digest(hashlib.sha256(csrf.encode()).hexdigest(), pending['csrf_hash'])):
            return reject('授权请求已失效，请重新连接。', 403)
        if fields['decision'] == 'deny':
            provider.pending.pop(ticket, None)
            params = pending['params']
            callback = construct_redirect_uri(str(params.redirect_uri), error='access_denied', state=params.state)
        elif fields['decision'] == 'approve':
            if not hmac.compare_digest(fields['admin_token'].encode(), owner_token.encode()):
                return reject('访问令牌错误，请返回授权页重试。', 401)
            callback = await provider.complete(ticket)
            if callback is None:
                return reject('授权请求已使用，请重新连接。', 403)
        else:
            return reject('请选择授权或取消。')
        response = RedirectResponse(callback, status_code=302, headers=headers)
        response.delete_cookie(COOKIE, path='/oauth/consent', secure=origin_parts.scheme == 'https',
                               httponly=True, samesite='lax')
        return response

    routes = create_auth_routes(provider, auth.issuer_url,
                                client_registration_options=auth.client_registration_options,
                                revocation_options=auth.revocation_options)
    # The SDK supports public PKCE clients but omits "none" from its metadata defaults.
    metadata = build_metadata(auth.issuer_url, None, auth.client_registration_options, auth.revocation_options)
    metadata.token_endpoint_auth_methods_supported = ['none', 'client_secret_post']
    metadata.revocation_endpoint_auth_methods_supported = ['none', 'client_secret_post']
    routes = [route for route in routes if route.path != '/.well-known/oauth-authorization-server']
    for path in ('/.well-known/oauth-authorization-server', '/.well-known/oauth-authorization-server/mcp'):
        routes.append(Route(path,
                            endpoint=cors_middleware(MetadataHandler(metadata).handle, ['GET', 'OPTIONS']),
                            methods=['GET', 'OPTIONS']))
    for route in routes:
        if route.path == '/revoke':
            route.app = PublicClientRevocationCompatibility(route.app)
    app.router.routes.extend(routes)
    resource_routes = create_protected_resource_routes(
        auth.resource_server_url, [auth.issuer_url], scopes_supported=SCOPES, resource_name='Fleet SSH')
    app.router.routes.extend(resource_routes)
    # Some connection forms probe the origin before reading the MCP 401 challenge.
    canonical = next(route for route in resource_routes if route.path == '/.well-known/oauth-protected-resource/mcp')
    app.router.routes.append(Route('/.well-known/oauth-protected-resource',
                                   endpoint=canonical.endpoint, methods=['GET', 'OPTIONS']))
    # The SDK parses token resource fields but does not pass them to the provider.
    app.add_middleware(TokenResourceMiddleware, resource_url=base + '/mcp')
    return provider
