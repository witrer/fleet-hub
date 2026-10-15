"""Fleet's browser OAuth flow, exercised through HTTP with synthetic credentials."""
import base64
import hashlib
from html.parser import HTMLParser
from pathlib import Path
import secrets
import sys
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).parent))
from fleet_oauth import install_oauth

BASE = 'https://hub.test'
RESOURCE = BASE + '/mcp'
OWNER = 'synthetic-owner-token-for-oauth-tests'
CALLBACK = 'https://chatgpt.com/connector/oauth/synthetic-callback-id'
SCOPE = 'mcp:tools'


class Inputs(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.values = {}
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'input' and values.get('name'):
            self.values[values['name']] = values.get('value', '')


@pytest.fixture(params=[False, True], ids=['shared-hub-origin', 'separate-oauth-origin'])
def oauth(tmp_path, request):
    app = FastAPI()
    config = {'base_url': BASE, 'admin_token': OWNER}
    if request.param:
        config.update(base_url='https://node-downloads.test', oauth_base_url=BASE)
    provider = install_oauth(app, config, tmp_path)
    assert app.state.oauth_provider is provider
    with TestClient(app, base_url=BASE, follow_redirects=False) as client:
        yield client


def register(client, **changes):
    response = client.post('/register', json={
        'client_name': 'Fleet synthetic browser',
        'redirect_uris': [CALLBACK],
        'token_endpoint_auth_method': 'none',
        'grant_types': ['authorization_code', 'refresh_token'],
        'response_types': ['code'],
        'scope': SCOPE,
        **changes,
    })
    assert response.status_code == 201
    assert OWNER not in response.text
    return response.json()


def authorization(client, registration=None, **changes):
    registration = registration or register(client)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    params = {
        'client_id': registration['client_id'], 'redirect_uri': CALLBACK,
        'response_type': 'code', 'scope': SCOPE, 'state': 'synthetic-oauth-state',
        'code_challenge': challenge, 'code_challenge_method': 'S256', 'resource': RESOURCE,
        **changes,
    }
    return registration, verifier, client.get('/authorize', params=params)


def consent_form(client, response):
    assert response.status_code == 302
    location = response.headers['location']
    assert 'code' not in parse_qs(urlsplit(location).query)
    assert urlsplit(location).path == '/oauth/consent'
    response = client.get(location)
    assert response.status_code == 200
    assert OWNER not in response.text
    cookie = response.headers.get('set-cookie', '').lower()
    assert 'secure' in cookie and 'httponly' in cookie and 'samesite=lax' in cookie
    fields = Inputs(response.text).values
    assert fields['ticket'] and fields['csrf']
    return {'ticket': fields['ticket'], 'csrf': fields['csrf'],
            'admin_token': OWNER, 'decision': 'approve'}


def approve(client, form):
    response = client.post('/oauth/consent', data=form, headers={'Origin': BASE})
    assert response.status_code in (302, 303)
    assert OWNER not in response.text and OWNER not in response.headers.get('location', '')
    location = urlsplit(response.headers['location'])
    expected = urlsplit(CALLBACK)
    assert (location.scheme, location.netloc, location.path) == (expected.scheme, expected.netloc, expected.path)
    query = parse_qs(location.query)
    assert query['state'] == ['synthetic-oauth-state']
    assert query.get('code') and 'error' not in query
    metadata = client.get('/.well-known/oauth-authorization-server').json()
    if metadata.get('authorization_response_iss_parameter_supported'):
        assert query['iss'] == [metadata['issuer']]
    return query['code'][0]


def code_grant(client):
    registration, verifier, response = authorization(client)
    form = consent_form(client, response)
    code = approve(client, form)
    return registration, verifier, code


def exchange(client, registration, verifier, code, **changes):
    credentials = {'client_secret': registration['client_secret']} if registration.get('client_secret') else {}
    return client.post('/token', data={
        'grant_type': 'authorization_code', 'client_id': registration['client_id'],
        'redirect_uri': CALLBACK, 'code': code, 'code_verifier': verifier,
        'resource': RESOURCE, **credentials, **changes,
    })


def authenticated(client, token):
    return client.get('/internal/mcp-auth', headers={'Authorization': 'Bearer ' + token})


def assert_oauth_error(response, expected):
    if response.status_code in (302, 303):
        fields = parse_qs(urlsplit(response.headers['location']).query)
        assert 'code' not in fields
        assert fields['error'] == [expected]
    else:
        assert response.status_code in (400, 401, 403)
        assert response.json()['error'] == expected


def test_discovery_dcr_and_original_admin_bearer(oauth):
    metadata = oauth.get('/.well-known/oauth-authorization-server')
    resource = oauth.get('/.well-known/oauth-protected-resource/mcp')
    assert metadata.status_code == resource.status_code == 200
    as_meta, rs_meta = metadata.json(), resource.json()
    assert as_meta['issuer'].rstrip('/') == BASE
    assert rs_meta['resource'] == RESOURCE
    assert rs_meta['authorization_servers'] == [as_meta['issuer']]
    assert as_meta['registration_endpoint'] == BASE + '/register'
    assert as_meta['authorization_endpoint'] == BASE + '/authorize'
    assert as_meta['token_endpoint'] == BASE + '/token'
    assert 'S256' in as_meta['code_challenge_methods_supported']
    assert 'none' in as_meta['token_endpoint_auth_methods_supported']
    assert 'refresh_token' in as_meta['grant_types_supported']
    assert OWNER not in metadata.text + resource.text
    for alias, expected in [('/.well-known/oauth-protected-resource', rs_meta),
                            ('/.well-known/oauth-authorization-server/mcp', as_meta)]:
        result = oauth.get(alias)
        assert result.status_code == 200 and result.json() == expected
        assert oauth.head(alias).status_code == 200
    registration = register(oauth)
    assert registration['token_endpoint_auth_method'] == 'none'
    assert not registration.get('client_secret')
    assert authenticated(oauth, OWNER).status_code == 204
    assert authenticated(oauth, OWNER).content == b''
    for headers in ({}, {'Authorization': 'Bearer incorrect-token'}):
        denied = oauth.get('/internal/mcp-auth', headers=headers)
        assert denied.status_code == 401
        assert 'resource_metadata=' in denied.headers['www-authenticate']
        assert '/.well-known/oauth-protected-resource/mcp' in denied.headers['www-authenticate']
        assert OWNER not in denied.text


def test_anonymous_authorize_requires_owner_consent(oauth):
    registration, verifier, response = authorization(oauth)
    form = consent_form(oauth, response)
    assert oauth.get('/internal/mcp-auth').status_code == 401
    denied = oauth.post('/oauth/consent', data={**form, 'admin_token': 'wrong-owner'}, headers={'Origin': BASE})
    assert denied.status_code in (400, 401, 403)
    assert OWNER not in denied.text and 'wrong-owner' not in denied.text
    assert 'code' not in parse_qs(urlsplit(denied.headers.get('location', '')).query)
    result = exchange(oauth, registration, verifier, 'not-an-issued-code')
    assert_oauth_error(result, 'invalid_grant')


@pytest.mark.parametrize('change', ['missing_csrf', 'wrong_csrf', 'missing_cookie', 'wrong_origin'])
def test_consent_rejects_csrf_and_origin(oauth, change):
    _, _, response = authorization(oauth)
    form = consent_form(oauth, response)
    origin = BASE
    if change == 'missing_csrf':
        form.pop('csrf')
    elif change == 'wrong_csrf':
        form['csrf'] = 'wrong-csrf'
    elif change == 'missing_cookie':
        oauth.cookies.clear()
    else:
        origin = 'https://untrusted.test'
    response = oauth.post('/oauth/consent', data=form, headers={'Origin': origin})
    assert response.status_code in (400, 401, 403, 422)
    assert OWNER not in response.text
    assert 'code' not in parse_qs(urlsplit(response.headers.get('location', '')).query)


def test_authorization_deny_returns_error_without_code(oauth):
    _, _, response = authorization(oauth)
    form = consent_form(oauth, response)
    response = oauth.post('/oauth/consent', data={**form, 'decision': 'deny'}, headers={'Origin': BASE})
    assert_oauth_error(response, 'access_denied')
    assert OWNER not in response.text


@pytest.mark.parametrize('auth_method', ['none', 'client_secret_post'])
def test_code_exchange_pkce_one_time_code_and_consent(oauth, auth_method):
    registration = register(oauth, token_endpoint_auth_method=auth_method)
    registration, verifier, response = authorization(oauth, registration)
    form = consent_form(oauth, response)
    code = approve(oauth, form)
    replay = oauth.post('/oauth/consent', data=form, headers={'Origin': BASE})
    assert replay.status_code in (400, 401, 403, 404, 410)
    if auth_method == 'client_secret_post':
        assert registration['client_secret']
        assert_oauth_error(exchange(oauth, registration, verifier, code, client_secret='wrong-secret'), 'invalid_client')
    assert_oauth_error(exchange(oauth, registration, 'wrong-verifier', code), 'invalid_grant')
    accepted = exchange(oauth, registration, verifier, code)
    assert accepted.status_code == 200
    result = accepted.json()
    assert result['token_type'].lower() == 'bearer' and result['expires_in'] > 0
    assert result['scope'].split() == [SCOPE]
    assert OWNER not in accepted.text
    assert authenticated(oauth, accepted.json()['access_token']).status_code == 204
    assert authenticated(oauth, accepted.json()['refresh_token']).status_code == 401
    assert_oauth_error(exchange(oauth, registration, verifier, code), 'invalid_grant')


def test_other_resource_and_ungranted_scope_are_rejected(oauth):
    _, _, response = authorization(oauth, resource='https://other.test/mcp')
    assert_oauth_error(response, 'invalid_target')
    _, _, response = authorization(oauth, scope='mcp:administrator')
    assert_oauth_error(response, 'invalid_scope')
    registration, verifier, code = code_grant(oauth)
    assert_oauth_error(exchange(oauth, registration, verifier, code, resource='https://other.test/mcp'), 'invalid_target')
    accepted = exchange(oauth, registration, verifier, code)
    assert accepted.status_code == 200


def test_refresh_rotation_scope_and_revocation(oauth):
    registration, verifier, code = code_grant(oauth)
    initial = exchange(oauth, registration, verifier, code).json()
    body = {'grant_type': 'refresh_token', 'client_id': registration['client_id'],
            'refresh_token': initial['refresh_token'], 'resource': RESOURCE}
    assert_oauth_error(oauth.post('/token', data={**body, 'scope': 'mcp:administrator'}), 'invalid_scope')
    assert_oauth_error(oauth.post('/token', data={**body, 'resource': 'https://other.test/mcp'}), 'invalid_target')
    response = oauth.post('/token', data=body)
    assert response.status_code == 200
    refreshed = response.json()
    assert refreshed['access_token'] != initial['access_token']
    assert refreshed['refresh_token'] != initial['refresh_token']
    assert refreshed['scope'].split() == [SCOPE]
    assert authenticated(oauth, refreshed['access_token']).status_code == 204
    assert_oauth_error(oauth.post('/token', data=body), 'invalid_grant')
    assert authenticated(oauth, refreshed['access_token']).status_code == 204
    for name, hint in [('access_token', 'access_token'), ('refresh_token', 'refresh_token')]:
        response = oauth.post('/revoke', data={
            'client_id': registration['client_id'], 'token': refreshed[name], 'token_type_hint': hint,
        })
        assert response.status_code == 200
        assert refreshed[name] not in response.text
    assert authenticated(oauth, refreshed['access_token']).status_code == 401
    assert_oauth_error(oauth.post('/token', data={**body, 'refresh_token': refreshed['refresh_token']}), 'invalid_grant')
    assert authenticated(oauth, OWNER).status_code == 204


def test_redirect_and_pkce_downgrade_are_rejected(oauth):
    _, _, response = authorization(oauth, redirect_uri='https://untrusted.test/steal')
    assert response.status_code == 400
    assert 'location' not in response.headers
    _, _, response = authorization(oauth, code_challenge_method='plain')
    assert_oauth_error(response, 'invalid_request')
