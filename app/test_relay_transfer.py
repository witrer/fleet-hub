"""Metadata orchestration tests. No account, credential file or external upload."""
import asyncio
import json
import time

import httpx
import pytest

from test_app import hub, ADMIN, body, grant
from onedrive import ProviderError


class Drive:
    enabled = True
    def __init__(self):
        self.items = {}
        self.sessions = {}
        self.remaining = 100000000
        self.deleted = []
        self.fail_delete = False
        self.session_missing = False
    async def quota(self): return {'remaining': self.remaining, 'total': self.remaining}
    async def find_item(self, name):
        if name not in self.items: raise ProviderError('not_found', 404)
        return self.items[name]
    async def create_upload(self, name, size):
        url = 'https://upload.test/' + name + '?secret=synthetic-upload'
        self.sessions[url] = dict(expires_at=time.time()+3600, next_expected_ranges=['0-'])
        return dict(upload_url=url, expires_at=self.sessions[url]['expires_at'])
    async def upload_status(self, url): return self.sessions[url]
    async def cancel_upload(self, url):
        if self.session_missing: raise ProviderError('not_found', 404)
        self.sessions.pop(url, None)
    async def create_empty(self, name): return self.put(name, 0)
    def put(self, name, size):
        result = dict(id='cloud-'+name, name=name, size=size, etag='"version-1"',
                      download_url='https://download.test/'+name+'?secret=synthetic-download')
        self.items[name] = result
        return result
    async def item(self, item_id):
        return next(item for item in self.items.values() if item['id'] == item_id)
    async def delete(self, *args, **kwargs):
        raise AssertionError('Cleanup must never silently fall back to the recycle bin')
    async def permanent_delete(self, item_id, *, expected_name, expected_etag):
        if self.fail_delete: raise ProviderError('throttled', 429, 1)
        item = await self.item(item_id)
        if item['name'] != expected_name or item['etag'] != expected_etag:
            raise ProviderError('item_changed', 409)
        self.deleted.append(item_id)
        self.items = {name: item for name, item in self.items.items() if item['id'] != item_id}
    async def close(self): pass


@pytest.fixture
def relay(hub, monkeypatch):
    module, client, _ = hub
    node = client.post('/api/enroll', headers=grant(client), json=body()).json()
    client.post('/api/nodes/'+node['id']+'/heartbeat', headers={'Authorization': 'Bearer '+node['token']},
                json={'relay_protocol': 1})
    provider = Drive()
    module.relay_transfers.provider = provider
    def no_ssh(*a, **kw): raise AssertionError('OneDrive tasks must not depend on an SSH tunnel')
    monkeypatch.setattr(module.asyncssh, 'connect', no_ssh)
    return module, client, node, provider


def payload(node, **changes):
    return dict(node_id=node['id'], name='dataset.bin', destination='/data/dataset.bin',
                is_directory=False, overwrite=False, directories=[],
                files=[dict(path='dataset.bin', size=16, mtime_ns=12345)],
                idempotency_key='synthetic-idempotency-key', **changes)


def create(relay, data=None):
    _, client, node, _ = relay
    response = client.post('/api/relay/transfers', headers=ADMIN, json=data or payload(node))
    assert response.status_code == 200, response.text
    return response.json()


def uploaded(relay, job):
    module, client, _, provider = relay
    for file in job['files']:
        record = module.relay_transfers.file(job['id'], file['id'])
        provider.put(record['object_name'], file['size'])
        assert client.post(f"/api/relay/transfers/{job['id']}/files/{file['id']}/complete", headers=ADMIN, json={}).json() == {'uploaded': True}
    return client.post(f"/api/relay/transfers/{job['id']}/ready", headers=ADMIN, json={})


def test_disabled_and_admin_auth(hub):
    _, client, _ = hub
    assert client.post('/api/relay/transfers', json={}).status_code == 401
    assert client.post('/api/relay/transfers', headers=ADMIN, json={}).status_code == 503


def test_creation_is_idempotent_quota_reserved_and_ssh_independent(relay):
    module, client, node, provider = relay
    job = create(relay)
    assert create(relay)['id'] == job['id']
    changed = payload(node)
    changed['destination'] = '/different'
    assert client.post('/api/relay/transfers', headers=ADMIN, json=changed).status_code == 409
    provider.remaining = 20
    second = payload(node)
    second['idempotency_key'] = 'synthetic-another-key'
    assert client.post('/api/relay/transfers', headers=ADMIN, json=second).status_code == 507
    assert module.db.execute('SELECT count(*) FROM relay_jobs').fetchone()[0] == 1


def test_old_node_cannot_queue_work_that_would_block_its_upgrade(relay):
    module, client, node, _ = relay
    auth = {'Authorization': 'Bearer '+node['token']}
    heartbeat = '/api/nodes/'+node['id']+'/heartbeat'
    for value in (None, True, 2, '1'):
        assert client.post(heartbeat, headers=auth, json={'relay_protocol': value}).json()['update_allowed'] is True
        assert client.post('/api/relay/transfers', headers=ADMIN, json=payload(node)).status_code == 409
    assert module.db.execute('SELECT count(*) FROM relay_jobs').fetchone()[0] == 0
    client.post(heartbeat, headers=auth, json={'relay_protocol': 1})
    assert create(relay)['status'] == 'uploading'


@pytest.mark.parametrize('path', ['../secret', '/absolute', 'a//b', 'a/./b', 'a\\b', ''])
def test_manifest_rejects_unsafe_paths(relay, path):
    _, client, node, _ = relay
    data = payload(node)
    data['files'][0]['path'] = path
    assert client.post('/api/relay/transfers', headers=ADMIN, json=data).status_code == 400


def test_manifest_size_and_file_directory_conflict(relay):
    _, client, node, _ = relay
    data = payload(node)
    data['files'][0]['size'] = 11 * 1024**3
    assert client.post('/api/relay/transfers', headers=ADMIN, json=data).status_code == 413
    data = payload(node)
    data.update(is_directory=True, directories=['dataset.bin/nested'])
    assert client.post('/api/relay/transfers', headers=ADMIN, json=data).status_code == 400


def test_rejected_session_can_retry_existing_task(relay, monkeypatch):
    module, client, _, provider = relay
    job = create(relay)
    file = job['files'][0]
    path = f"/api/relay/transfers/{job['id']}/files/{file['id']}/session"
    create_upload = provider.create_upload

    async def rejected(name, size):
        raise ProviderError('provider_rejected', 400)

    monkeypatch.setattr(provider, 'create_upload', rejected)
    assert client.post(path, headers=ADMIN, json={}).status_code == 400
    assert module.relay_transfers.file(job['id'], file['id'])['upload_url'] is None
    assert client.get('/api/relay/transfers/'+job['id'], headers=ADMIN).json()['status'] == 'uploading'

    monkeypatch.setattr(provider, 'create_upload', create_upload)
    response = client.post(path, headers=ADMIN, json={})
    assert response.status_code == 200
    assert response.json()['next_offset'] == 0
    assert create(relay)['id'] == job['id']
    assert module.db.execute('SELECT count(*) FROM relay_jobs').fetchone()[0] == 1


def test_session_resume_expiry_and_lost_final_response(relay):
    module, client, _, provider = relay
    job = create(relay)
    file = job['files'][0]
    path = f"/api/relay/transfers/{job['id']}/files/{file['id']}/session"
    session = client.post(path, headers=ADMIN, json={}).json()
    assert session['next_offset'] == 0
    provider.sessions[session['upload_url']]['next_expected_ranges'] = ['8-']
    assert client.post(path, headers=ADMIN, json={}).json()['next_offset'] == 8
    module.db.execute('UPDATE relay_files SET upload_expires=0')
    module.db.commit()
    assert client.post(path, headers=ADMIN, json={}).json()['next_offset'] == 0
    record = module.relay_transfers.file(job['id'], file['id'])
    provider.put(record['object_name'], file['size'])
    # Final OneDrive commit survived even though the client received no response.
    assert client.post(path, headers=ADMIN, json={}).json() == {'uploaded': True}


def test_ready_requires_verified_cloud_object_not_client_progress(relay):
    module, client, _, provider = relay
    job = create(relay)
    file = job['files'][0]
    base = f"/api/relay/transfers/{job['id']}"
    assert client.post(base+'/ready', headers=ADMIN, json={}).status_code == 409
    assert client.post(base+f"/files/{file['id']}/upload-progress", headers=ADMIN, json={'bytes_done': 16}).status_code == 200
    assert client.post(base+'/ready', headers=ADMIN, json={}).status_code == 409
    record = module.relay_transfers.file(job['id'], file['id'])
    provider.put(record['object_name'], 15)
    assert client.post(base+f"/files/{file['id']}/complete", headers=ADMIN, json={}).status_code == 409
    assert uploaded(relay, job).json()['status'] == 'ready'


def test_node_queue_redirect_and_final_confirmation(relay):
    module, client, node, provider = relay
    auth = {'Authorization': 'Bearer ' + node['token']}
    heartbeat = '/api/nodes/'+node['id']+'/heartbeat'
    queue = '/api/nodes/'+node['id']+'/relay-task'
    job = create(relay)
    file = job['files'][0]
    beat = client.post(heartbeat, headers=auth, json={}).json()
    assert beat['update_allowed'] is False and beat['relay_available'] is False
    assert client.get(queue, headers=auth).json()['task'] is None
    uploaded(relay, job)
    assert client.post(heartbeat, headers=auth, json={}).json()['relay_available'] is True
    task = client.get(queue, headers=auth).json()['task']
    path = task['files'][0]['download_path']
    assert client.get(path, follow_redirects=False).status_code == 401
    response = client.get(path, headers=auth, follow_redirects=False)
    assert response.status_code == 302 and response.content == b''
    assert response.headers['location'].startswith('https://download.test/')
    assert response.headers['cache-control'] == 'no-store'
    # Dashboard/MCP metadata never includes signed cloud URLs or refresh credentials.
    state = client.get('/api/state', headers=ADMIN).json()
    assert state['storage']['onedrive']['enabled'] is True
    assert 'synthetic-download' not in json.dumps(state) and 'synthetic-upload' not in json.dumps(state)
    progress = '/api/nodes/'+node['id']+'/relay-tasks/'+job['id']+'/progress'
    assert client.post(progress, headers=auth, json={'status': 'downloading', 'file_id': None, 'bytes_done': 0}).status_code == 200
    assert client.post(progress, headers=auth, json={'status': 'completed', 'file_id': None, 'bytes_done': 0}).status_code == 409
    assert client.post(progress, headers=auth, json={'status': 'downloading', 'file_id': file['id'], 'bytes_done': 12}).status_code == 200
    assert client.post(progress, headers=auth, json={'status': 'downloading', 'file_id': file['id'], 'bytes_done': 2}).status_code == 200
    assert module.relay_transfers.public(module.relay_transfers.job(job['id']))['bytes_done'] == 12
    assert client.post(progress, headers=auth, json={'status': 'completed', 'file_id': file['id'], 'bytes_done': 16}).status_code == 200
    assert client.post(progress, headers=auth, json={'status': 'completed', 'file_id': None, 'bytes_done': 0}).status_code == 200
    assert module.relay_transfers.job(job['id'])['status'] == 'completed'
    assert client.post(heartbeat, headers=auth, json={}).json()['update_allowed'] is True
    provider.fail_delete = True
    asyncio.run(module.relay_transfers.cleanup_once())
    assert module.relay_transfers.job(job['id'])['cleanup_pending'] == 1
    provider.fail_delete = False
    module.db.execute('UPDATE relay_jobs SET cleanup_after=0')
    module.db.execute('UPDATE relay_files SET cleanup_after=0')
    module.db.commit()
    asyncio.run(module.relay_transfers.cleanup_once())
    assert module.relay_transfers.job(job['id'])['cleanup_pending'] == 0 and len(provider.deleted) == 1
    assert client.post(progress, headers=auth, json={'status': 'failed', 'error': 'late_failure'}).json() == {'ok': True}
    assert module.relay_transfers.job(job['id'])['status'] == 'completed'


def test_wrong_node_revocation_and_changed_etag(relay):
    module, client, node, provider = relay
    job = create(relay)
    uploaded(relay, job)
    second = client.post('/api/enroll', headers=grant(client), json=body()).json()
    file = job['files'][0]
    path = f"/api/relay/transfers/{job['id']}/files/{file['id']}/content"
    auth = {'Authorization': 'Bearer '+node['token']}
    wrong = {'Authorization': 'Bearer '+second['token']}
    assert client.get(path, headers=wrong, follow_redirects=False).status_code == 401
    target = next(iter(provider.items.values()))
    target['etag'] = '"changed"'
    assert client.get(path, headers=auth, follow_redirects=False).status_code == 409
    target['etag'] = '"version-1"'
    client.post('/api/nodes/'+node['id']+'/revoke', headers=ADMIN)
    assert client.get(path, headers=ADMIN, follow_redirects=False).status_code == 410
    asyncio.run(module.relay_transfers.cleanup_once())
    assert module.relay_transfers.job(job['id'])['status'] == 'cancelled'


@pytest.mark.parametrize('with_session', [False, True])
def test_cleanup_finds_committed_object_after_lost_creation_response(relay, with_session):
    module, client, _, provider = relay
    job = create(relay)
    file = job['files'][0]
    if with_session:
        client.post(f"/api/relay/transfers/{job['id']}/files/{file['id']}/session", headers=ADMIN, json={})
    record = module.relay_transfers.file(job['id'], file['id'])
    provider.put(record['object_name'], file['size'])
    provider.session_missing = True
    client.post(f"/api/relay/transfers/{job['id']}/cancel", headers=ADMIN, json={})
    asyncio.run(module.relay_transfers.cleanup_once())
    assert len(provider.deleted) == 1 and not provider.items
    assert module.relay_transfers.job(job['id'])['cleanup_pending'] == 0


def test_cancel_during_session_status_does_not_return_upload_capability(relay):
    module, client, _, provider = relay
    job = create(relay)
    file = job['files'][0]
    path = f"/api/relay/transfers/{job['id']}/files/{file['id']}/session"
    client.post(path, headers=ADMIN, json={})
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        original = provider.upload_status
        async def blocked(url):
            entered.set()
            await release.wait()
            return await original(url)
        provider.upload_status = blocked
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url='https://hub.test') as local:
            request = asyncio.create_task(local.post(path, headers=ADMIN, json={}))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                assert (await local.post(f"/api/relay/transfers/{job['id']}/cancel", headers=ADMIN, json={})).status_code == 200
            finally:
                release.set()
                response = await asyncio.wait_for(request, 2)
            assert response.status_code == 410 and 'upload_url' not in response.text
    asyncio.run(scenario())


def test_empty_directory_and_zero_byte_file(relay):
    module, client, node, _ = relay
    data = payload(node)
    data.update(is_directory=True, files=[], directories=['', 'empty'])
    job = create(relay, data)
    assert uploaded(relay, job).status_code == 200
    auth = {'Authorization': 'Bearer '+node['token']}
    progress = f"/api/nodes/{node['id']}/relay-tasks/{job['id']}/progress"
    assert client.post(progress, headers=auth, json={'file_id': None, 'status': 'completed', 'bytes_done': 0}).status_code == 200
    data = payload(node)
    data['idempotency_key'] = 'synthetic-empty-file-key'
    data['files'][0]['size'] = 0
    job = create(relay, data)
    path = f"/api/relay/transfers/{job['id']}/files/{job['files'][0]['id']}/session"
    assert client.post(path, headers=ADMIN, json={}).json() == {'uploaded': True}


def test_expiry_is_terminal_and_cleanup_is_persisted(relay):
    module, client, _, _ = relay
    job = create(relay)
    module.db.execute('UPDATE relay_jobs SET expires=0 WHERE id=?', (job['id'],))
    module.db.commit()
    assert client.post(f"/api/relay/transfers/{job['id']}/ready", headers=ADMIN, json={}).status_code == 410
    asyncio.run(module.relay_transfers.cleanup_once())
    assert module.relay_transfers.job(job['id'])['status'] == 'expired'


def test_changed_objects_are_not_deleted_and_do_not_starve_later_cleanup(relay):
    module, client, node, provider = relay
    jobs = []
    for index in range(9):
        data = payload(node)
        data['idempotency_key'] = 'synthetic-cleanup-key-'+str(index)
        job = create(relay, data)
        uploaded(relay, job)
        record = module.relay_transfers.file(job['id'], job['files'][0]['id'])
        if index < 8:
            provider.items[record['object_name']]['etag'] = '"externally-changed"'
        client.post(f"/api/relay/transfers/{job['id']}/cancel", headers=ADMIN, json={})
        jobs.append(job)
    asyncio.run(module.relay_transfers.cleanup_once())
    assert not provider.deleted
    assert all(module.relay_transfers.job(j['id'])['cleanup_error'] == 'object_changed' for j in jobs[:8])
    asyncio.run(module.relay_transfers.cleanup_once())
    assert len(provider.deleted) == 1
    assert module.relay_transfers.job(jobs[-1]['id'])['cleanup_pending'] == 0


def test_revocation_during_quota_check_does_not_create_a_task(relay):
    module, client, node, provider = relay
    async def quota():
        module.db.execute('UPDATE nodes SET active=0 WHERE id=?', (node['id'],))
        module.db.commit()
        return {'remaining': 1000000}
    provider.quota = quota
    assert client.post('/api/relay/transfers', headers=ADMIN, json=payload(node)).status_code == 410
    assert module.db.execute('SELECT count(*) FROM relay_jobs').fetchone()[0] == 0


def test_cleanup_is_permanent_and_reports_persistent_outcome(relay):
    module, client, _, provider = relay
    job = create(relay)
    uploaded(relay, job)
    client.post(f"/api/relay/transfers/{job['id']}/cancel", headers=ADMIN, json={})
    before = module.relay_transfers.cleanup_summary()
    assert before['mode'] == 'permanent' and before['scope'] == 'fleet_temp_only'
    assert before['pending_count'] == 1 and before['last_attempt_at'] is None
    asyncio.run(module.relay_transfers.cleanup_once())
    after = module.relay_transfers.cleanup_summary()
    assert len(provider.deleted) == 1
    assert after['pending_count'] == 0 and after['error_count'] == 0
    assert after['last_success_at'] >= after['last_attempt_at']
    assert after['legacy_recycle_bin'] == 'manual'


def test_missing_bound_object_does_not_claim_permanent_cleanup(relay):
    module, client, _, provider = relay
    job = create(relay)
    uploaded(relay, job)
    client.post(f"/api/relay/transfers/{job['id']}/cancel", headers=ADMIN, json={})
    async def missing(*args, **kwargs):
        raise ProviderError('cleanup_item_unavailable', 404)
    provider.permanent_delete = missing
    asyncio.run(module.relay_transfers.cleanup_once())
    summary = module.relay_transfers.cleanup_summary()
    assert summary['pending_count'] == summary['error_count'] == 1
    assert summary['error_code'] == 'object_unavailable' and summary['last_success_at'] is None


def test_account_change_cannot_receive_or_cleanup_old_objects(relay):
    module, client, node, provider = relay
    job = create(relay)
    uploaded(relay, job)
    module.relay_transfers.config = {'drive_id': 'different-drive', 'folder_id': 'different-folder'}
    content = f"/api/relay/transfers/{job['id']}/files/{job['files'][0]['id']}/content"
    assert client.get(content, headers=ADMIN, follow_redirects=False).status_code == 409
    client.post(f"/api/relay/transfers/{job['id']}/cancel", headers=ADMIN, json={})
    asyncio.run(module.relay_transfers.cleanup_once())
    summary = module.relay_transfers.cleanup_summary()
    assert summary['pending_count'] == 1 and summary['error_code'] == 'account_changed'
    assert provider.deleted == []


def test_cleanup_rotates_past_changed_files_within_a_large_task(relay):
    module, client, node, provider = relay
    data = payload(node)
    data.update(is_directory=True, files=[{'path': f'{i:02}.bin', 'size': 1, 'mtime_ns': 1} for i in range(33)])
    job = create(relay, data)
    uploaded(relay, job)
    for file in job['files'][:32]:
        record = module.relay_transfers.file(job['id'], file['id'])
        provider.items[record['object_name']]['etag'] = '"modified"'
    client.post(f"/api/relay/transfers/{job['id']}/cancel", headers=ADMIN, json={})
    asyncio.run(module.relay_transfers.cleanup_once())
    assert provider.deleted == []
    module.db.execute('UPDATE relay_jobs SET cleanup_after=0')
    module.db.commit()
    asyncio.run(module.relay_transfers.cleanup_once())
    assert len(provider.deleted) == 1
    assert module.relay_transfers.file(job['id'], job['files'][-1]['id'])['cleaned'] == 1
    assert module.relay_transfers.job(job['id'])['cleanup_pending'] == 1
