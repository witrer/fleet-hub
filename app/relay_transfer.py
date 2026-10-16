"""OneDrive transfer metadata. File bytes always travel between endpoints and OneDrive."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import secrets
import time
from pathlib import PurePosixPath

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from onedrive import OneDrive, ProviderError

ACTIVE = ('uploading', 'ready', 'downloading')
TERMINAL = ('completed', 'failed', 'expired', 'cancelled')
MAX_BODY = 4 * 1024 * 1024


async def body(request):
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_BODY:
            raise HTTPException(413, 'Transfer metadata is too large')
    try:
        result = json.loads(content)
    except (ValueError, UnicodeError):
        raise HTTPException(400, 'Invalid JSON') from None
    if not isinstance(result, dict):
        raise HTTPException(400, 'Expected an object')
    return result


def relative_path(value, directory=False):
    if not isinstance(value, str) or len(value) > 4096 or '\\' in value or '\x00' in value:
        raise HTTPException(400, 'Invalid relative file path')
    if directory and value == '':
        return value
    parts = value.split('/')
    if not value or PurePosixPath(value).is_absolute() or any(p in ('', '.', '..') for p in parts):
        raise HTTPException(400, 'Invalid relative file path')
    return value


class RelayTransfers:
    def __init__(self, db, config, root):
        self.db = db
        self.config = config.get('onedrive', {})
        self.provider = OneDrive(self.config, root / 'onedrive-token.json')
        self.create_lock = asyncio.Lock()
        self.locks = {}
        db.executescript('''
        CREATE TABLE IF NOT EXISTS relay_jobs(
          id TEXT PRIMARY KEY,node_id TEXT NOT NULL,name TEXT NOT NULL,destination TEXT NOT NULL,
          is_directory INTEGER NOT NULL,overwrite INTEGER NOT NULL,manifest TEXT NOT NULL,
          manifest_hash TEXT NOT NULL,idempotency_key TEXT UNIQUE NOT NULL,status TEXT NOT NULL,
          created REAL NOT NULL,updated REAL NOT NULL,expires REAL NOT NULL,error TEXT NOT NULL DEFAULT '',
          cleanup_pending INTEGER NOT NULL DEFAULT 0,cleanup_after REAL NOT NULL DEFAULT 0,
          cleanup_error TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS relay_files(
          id TEXT PRIMARY KEY,job_id TEXT NOT NULL,path TEXT NOT NULL,size INTEGER NOT NULL,
          mtime_ns INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',object_name TEXT NOT NULL,
          upload_url TEXT,upload_expires REAL,item_id TEXT,etag TEXT,downloaded INTEGER NOT NULL DEFAULT 0,
          cleaned INTEGER NOT NULL DEFAULT 0,uploaded INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS relay_job_nodes ON relay_jobs(node_id,status,created);
        CREATE INDEX IF NOT EXISTS relay_file_jobs ON relay_files(job_id);
        ''')
        if 'uploaded' not in {r[1] for r in db.execute('PRAGMA table_info(relay_files)')}:
            db.execute('ALTER TABLE relay_files ADD COLUMN uploaded INTEGER NOT NULL DEFAULT 0')
        if 'cleanup_after' not in {r[1] for r in db.execute('PRAGMA table_info(relay_files)')}:
            db.execute('ALTER TABLE relay_files ADD COLUMN cleanup_after REAL NOT NULL DEFAULT 0')
        columns = {r[1] for r in db.execute('PRAGMA table_info(relay_jobs)')}
        for field, kind in (('cleanup_after', 'REAL NOT NULL DEFAULT 0'), ('cleanup_error', "TEXT NOT NULL DEFAULT ''")):
            if field not in columns:
                db.execute(f'ALTER TABLE relay_jobs ADD COLUMN {field} {kind}')
        for field in ('cleanup_attempted', 'cleanup_completed'):
            if field not in columns:
                db.execute(f'ALTER TABLE relay_jobs ADD COLUMN {field} REAL')
        if 'storage_binding' not in columns:
            db.execute("ALTER TABLE relay_jobs ADD COLUMN storage_binding TEXT NOT NULL DEFAULT ''")
            db.execute('UPDATE relay_jobs SET storage_binding=?', (self.storage_binding(),))
        db.commit()

    @property
    def enabled(self):
        return self.provider.enabled

    def lock(self, task_id):
        return self.locks.setdefault(task_id, asyncio.Lock())

    def storage_binding(self, config=None):
        config = self.config if config is None else config
        return json.dumps([config.get('drive_id', ''), config.get('folder_id', '')], separators=(',', ':'))

    def account_busy(self):
        return bool(self.db.execute("SELECT 1 FROM relay_jobs WHERE cleanup_pending=1 OR "
                                    "status IN ('uploading','ready','downloading') LIMIT 1").fetchone())

    def cleanup_summary(self):
        row = self.db.execute('SELECT COALESCE(SUM(cleanup_pending),0),'
                              "COALESCE(SUM(cleanup_pending=1 AND cleanup_error!=''),0),"
                              'MAX(cleanup_attempted),MAX(cleanup_completed) FROM relay_jobs').fetchone()
        error = self.db.execute("SELECT cleanup_error FROM relay_jobs WHERE cleanup_pending=1 AND cleanup_error!='' "
                                'ORDER BY cleanup_attempted DESC LIMIT 1').fetchone()
        return dict(mode='permanent', scope='fleet_temp_only', pending_count=row[0], error_count=row[1],
                    last_attempt_at=row[2], last_success_at=row[3], error_code=error[0] if error else None,
                    legacy_recycle_bin='manual')

    def job(self, task_id, active=False):
        row = self.db.execute('SELECT * FROM relay_jobs WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise HTTPException(404, 'Transfer not found')
        if active:
            if row['storage_binding'] != self.storage_binding():
                raise HTTPException(409, 'OneDrive account or staging folder changed')
            if row['expires'] <= time.time() or row['status'] in ('expired', 'cancelled'):
                raise HTTPException(410, 'Transfer authorization expired or cancelled')
            if not self.db.execute('SELECT 1 FROM nodes WHERE id=? AND active=1', (row['node_id'],)).fetchone():
                raise HTTPException(410, 'Node was revoked')
            if row['status'] == 'failed':
                raise HTTPException(409, 'Transfer has failed')
        return row

    def file(self, task_id, file_id):
        row = self.db.execute('SELECT * FROM relay_files WHERE id=? AND job_id=?', (file_id, task_id)).fetchone()
        if row is None:
            raise HTTPException(404, 'Transfer file not found')
        return row

    def public(self, row, files=False):
        items = self.db.execute('SELECT * FROM relay_files WHERE job_id=? ORDER BY rowid', (row['id'],)).fetchall()
        total = sum(f['size'] for f in items)
        downloaded = sum(f['downloaded'] for f in items)
        result = dict(id=row['id'], node_id=row['node_id'], name=row['name'], mode='onedrive',
                      status=row['status'], phase=row['status'], bytes_total=total, bytes_done=downloaded,
                      uploaded_bytes=sum(f['size'] if f['item_id'] else f['uploaded'] for f in items), downloaded_bytes=downloaded,
                      speed_bps=0, updated_at=row['updated'], expires=row['expires'], error=row['error'],
                      cleanup_pending=bool(row['cleanup_pending']),
                      cleanup_error=row['cleanup_error'],
                      progress_stale=row['status'] in ACTIVE and time.time()-row['updated'] > 90,
                      authorization_expired=row['expires'] <= time.time())
        if files:
            result['files'] = [dict(id=f['id'], path=f['path'], size=f['size'], status=f['status']) for f in items]
        return result

    def recent(self):
        return [self.public(row) for row in self.db.execute('SELECT * FROM relay_jobs ORDER BY updated DESC LIMIT 50')]

    def available(self, node_id):
        return bool(self.db.execute("SELECT 1 FROM relay_jobs WHERE node_id=? AND expires>? "
                                    "AND status IN ('ready','downloading') LIMIT 1", (node_id, time.time())).fetchone())

    def busy(self, node_id):
        return bool(self.db.execute("SELECT 1 FROM relay_jobs WHERE node_id=? AND expires>? "
                                    "AND status IN ('uploading','ready','downloading') LIMIT 1", (node_id, time.time())).fetchone())

    async def call(self, method, *args, **kwargs):
        try:
            async with asyncio.timeout(30):
                return await getattr(self.provider, method)(*args, **kwargs)
        except ProviderError as exc:
            status = exc.status if exc.status in (400, 401, 403, 404, 409, 410, 412, 429, 503, 507) else 502
            headers = {'Retry-After': str(math.ceil(exc.retry_after))} if exc.retry_after is not None else None
            raise HTTPException(status, 'OneDrive: ' + exc.code, headers=headers) from None
        except TimeoutError:
            raise HTTPException(504, 'OneDrive metadata request timed out') from None

    async def create(self, data):
        if not self.enabled:
            raise HTTPException(503, 'OneDrive is not configured by the administrator')
        node = data.get('node_id')
        if not isinstance(node, str) or not self.db.execute('SELECT 1 FROM nodes WHERE id=? AND active=1', (node,)).fetchone():
            raise HTTPException(404, 'Active node not found')
        metrics = json.loads(self.db.execute('SELECT metrics FROM nodes WHERE id=?', (node,)).fetchone()[0])
        if metrics.get('relay_protocol') != 1:
            raise HTTPException(409, 'Wait for the node agent to update before using OneDrive')
        destination = data.get('destination')
        if not isinstance(destination, str) or not destination.strip() or '\x00' in destination or len(destination) > 4096:
            raise HTTPException(400, 'Invalid destination')
        if type(data.get('is_directory')) is not bool or type(data.get('overwrite', False)) is not bool:
            raise HTTPException(400, 'Invalid file options')
        key = data.get('idempotency_key')
        if not isinstance(key, str) or not re.fullmatch('[A-Za-z0-9_-]{16,128}', key):
            raise HTTPException(400, 'Invalid idempotency key')
        files, directories = data.get('files'), data.get('directories', [])
        if not isinstance(files, list) or not isinstance(directories, list) or len(files)+len(directories) > 10000:
            raise HTTPException(400, 'Too many files or invalid manifest')
        manifest_files, names = [], set()
        for item in files:
            if not isinstance(item, dict):
                raise HTTPException(400, 'Invalid manifest file')
            path = relative_path(item.get('path'))
            size, mtime = item.get('size'), item.get('mtime_ns', 0)
            if path in names or type(size) is not int or size < 0 or type(mtime) is not int or not 0 <= mtime <= 2**63-1:
                raise HTTPException(400, 'Invalid file size, time or duplicate path')
            names.add(path)
            manifest_files.append(dict(path=path, size=size, mtime_ns=mtime))
        dirs = list(dict.fromkeys(relative_path(d, True) for d in directories))
        if names.intersection(dirs) or any('/'.join(path.split('/')[:i]) in names
                                           for path in names | set(dirs) for i in range(1, len(path.split('/')))):
            raise HTTPException(400, 'Manifest file and directory paths conflict')
        if not data['is_directory'] and (len(files) != 1 or dirs):
            raise HTTPException(400, 'A file source must contain exactly one file')
        total = sum(f['size'] for f in manifest_files)
        if total > int(self.config.get('max_task_bytes', 10 * 1024**3)):
            raise HTTPException(413, 'Transfer exceeds configured size limit')
        manifest = dict(node_id=node, destination=destination, overwrite=data.get('overwrite', False),
                        is_directory=data['is_directory'], files=manifest_files, directories=dirs)
        encoded = json.dumps(manifest, sort_keys=True, separators=(',', ':'))
        # IDs, download paths and normal etags expand the receive manifest. Keep
        # headroom now; ready() also checks the exact envelope before release.
        if len(encoded.encode()) + len(files) * 400 > MAX_BODY:
            raise HTTPException(413, 'Transfer manifest is too large; use fewer files per task')
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        async with self.create_lock:
            old = self.db.execute('SELECT * FROM relay_jobs WHERE idempotency_key=?', (key,)).fetchone()
            if old:
                if old['manifest_hash'] != fingerprint:
                    raise HTTPException(409, 'Source or destination changed for this transfer')
                return self.public(old, True)
            active = self.db.execute("SELECT count(*) FROM relay_jobs WHERE status IN ('uploading','ready','downloading')").fetchone()[0]
            if active >= int(self.config.get('max_active_tasks', 8)):
                raise HTTPException(429, 'Too many active OneDrive transfers')
            quota = await self.call('quota')
            if not self.db.execute('SELECT 1 FROM nodes WHERE id=? AND active=1', (node,)).fetchone():
                raise HTTPException(410, 'Node was revoked while preparing the transfer')
            reserved = self.db.execute("SELECT COALESCE(sum(f.size),0) FROM relay_files f JOIN relay_jobs j ON j.id=f.job_id "
                                       "WHERE j.status IN ('uploading','ready','downloading') AND f.item_id IS NULL").fetchone()[0]
            if total + reserved > quota['remaining']:
                raise HTTPException(507, 'OneDrive has insufficient available space')
            task_id = 'relay-' + secrets.token_hex(10)
            now = time.time()
            self.db.execute('''INSERT INTO relay_jobs(id,node_id,name,destination,is_directory,overwrite,manifest,
                manifest_hash,idempotency_key,status,created,updated,expires,storage_binding) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (task_id, node, str(data.get('name', 'upload'))[:160], destination, int(data['is_directory']),
                 int(data.get('overwrite', False)), encoded, fingerprint, key, 'uploading', now, now, now+86400,
                 self.storage_binding()))
            for f in manifest_files:
                file_id = 'file-' + secrets.token_hex(8)
                self.db.execute('INSERT INTO relay_files(id,job_id,path,size,mtime_ns,object_name) VALUES(?,?,?,?,?,?)',
                                (file_id, task_id, f['path'], f['size'], f['mtime_ns'], task_id+'-'+file_id))
            self.db.commit()
            return self.public(self.job(task_id), True)

    async def find(self, file):
        try:
            return await self.call('find_item', file['object_name'])
        except HTTPException as exc:
            if exc.status_code == 404:
                return None
            raise

    def accept_item(self, task_id, file, item):
        self.job(task_id, True)  # Node revocation/cancellation may happen during Graph I/O.
        if item['name'] != file['object_name'] or item['size'] != file['size'] or not item.get('etag'):
            raise HTTPException(409, 'OneDrive object does not match the transfer')
        if file['item_id'] and (file['item_id'] != item['id'] or file['etag'] != item['etag']):
            raise HTTPException(409, 'OneDrive object changed')
        self.db.execute("UPDATE relay_files SET item_id=?,etag=?,status='uploaded',upload_url=NULL,upload_expires=NULL "
                        "WHERE id=? AND status NOT IN ('downloading','completed')", (item['id'], item['etag'], file['id']))
        self.db.execute('UPDATE relay_jobs SET updated=? WHERE id=?', (time.time(), task_id))
        self.db.commit()

    async def session(self, task_id, file_id):
        async with self.lock(task_id):
            job = self.job(task_id, True)
            file = self.file(task_id, file_id)
            if file['item_id']:
                return {'uploaded': True}
            if job['status'] != 'uploading':
                raise HTTPException(409, 'Transfer is not accepting uploads')
            # An active upload can expose a zero-byte placeholder under its final
            # name. Reconcile the session before treating an object as complete.
            # PUTs also extend the remote expiry without updating our cached time.
            if file['upload_url']:
                try:
                    current = await self.call('upload_status', file['upload_url'])
                except HTTPException as exc:
                    if exc.status_code not in (404, 410):
                        raise
                else:
                    self.job(task_id, True)
                    ranges = current['next_expected_ranges']
                    try:
                        offset = min(int(value.split('-')[0]) for value in ranges)
                        if not 0 <= offset <= file['size']:
                            raise ValueError()
                    except (ValueError, TypeError):
                        raise HTTPException(502, 'Invalid OneDrive upload offset') from None
                    self.db.execute('UPDATE relay_files SET upload_expires=? WHERE id=?', (current['expires_at'], file_id))
                    self.db.commit()
                    return dict(upload_url=file['upload_url'], expires_at=current['expires_at'], next_offset=offset)
            # A missing session may mean the final PUT committed but its reply
            # was lost. Keep strict validation; never accept or overwrite a
            # remaining placeholder as a completed nonempty file.
            item = await self.find(file)
            if item is not None:
                self.accept_item(task_id, file, item)
                return {'uploaded': True}
            if file['size'] == 0:
                item = await self.call('create_empty', file['object_name'])
                self.accept_item(task_id, file, item)
                return {'uploaded': True}
            session = await self.call('create_upload', file['object_name'], file['size'])
            self.job(task_id, True)
            self.db.execute("UPDATE relay_files SET upload_url=?,upload_expires=?,status='uploading' WHERE id=?",
                            (session['upload_url'], session['expires_at'], file_id))
            self.db.execute('UPDATE relay_jobs SET updated=? WHERE id=?', (time.time(), task_id))
            self.db.commit()
            return dict(session, next_offset=0)

    async def complete_file(self, task_id, file_id):
        async with self.lock(task_id):
            self.job(task_id, True)
            file = self.file(task_id, file_id)
            if file['item_id']:
                return {'uploaded': True}
            item = await self.find(file)
            if item is None:
                raise HTTPException(409, 'OneDrive upload is not complete')
            self.accept_item(task_id, file, item)
            return {'uploaded': True}

    def upload_progress(self, task_id, file_id, data):
        job = self.job(task_id, True)
        file = self.file(task_id, file_id)
        done = data.get('bytes_done')
        if type(done) is not int or not 0 <= done <= file['size']:
            raise HTTPException(400, 'Invalid upload progress')
        if job['status'] == 'uploading':
            self.db.execute('UPDATE relay_files SET uploaded=MAX(uploaded,?) WHERE id=?', (done, file_id))
            self.db.execute('UPDATE relay_jobs SET updated=? WHERE id=?', (time.time(), task_id))
            self.db.commit()
        return {'ok': True}

    def ready(self, task_id):
        row = self.job(task_id, True)
        if row['status'] == 'uploading':
            if self.db.execute('SELECT 1 FROM relay_files WHERE job_id=? AND item_id IS NULL LIMIT 1', (task_id,)).fetchone():
                raise HTTPException(409, 'Some files have not finished uploading')
            if len(json.dumps(self.envelope(row)).encode()) > MAX_BODY:
                self.db.execute("UPDATE relay_jobs SET status='failed',error='manifest_too_large',cleanup_pending=1,updated=? WHERE id=?",
                                (time.time(), task_id))
                self.db.commit()
                raise HTTPException(413, 'Receive manifest is too large; split the source into fewer files')
            self.db.execute("UPDATE relay_jobs SET status='ready',updated=? WHERE id=? AND status='uploading'", (time.time(), task_id))
            self.db.commit()
        return self.public(self.job(task_id), True)

    def next_task(self, node_id):
        row = self.db.execute("SELECT * FROM relay_jobs WHERE node_id=? AND expires>? AND status IN ('ready','downloading') "
                              "ORDER BY created LIMIT 1", (node_id, time.time())).fetchone()
        if row is None:
            return {'task': None}
        self.db.execute("UPDATE relay_jobs SET status='downloading',updated=? WHERE id=?", (time.time(), row['id']))
        self.db.commit()
        return self.envelope(row)

    def envelope(self, row):
        manifest = json.loads(row['manifest'])
        files = [dict(id=f['id'], path=f['path'], size=f['size'], etag=f['etag'], status=f['status'],
                      download_path=f"/api/relay/transfers/{row['id']}/files/{f['id']}/content")
                 for f in self.db.execute('SELECT * FROM relay_files WHERE job_id=? ORDER BY rowid', (row['id'],))]
        return {'task': dict(id=row['id'], destination=row['destination'], overwrite=bool(row['overwrite']),
                             is_directory=bool(row['is_directory']), directories=manifest['directories'], files=files)}

    async def content(self, task_id, file_id):
        job = self.job(task_id, True)
        file = self.file(task_id, file_id)
        if job['status'] not in ('ready', 'downloading') or not file['item_id'] or file['cleaned']:
            raise HTTPException(409, 'File is not available for download')
        item = await self.call('item', file['item_id'])
        self.job(task_id, True)
        if item['size'] != file['size'] or item['etag'] != file['etag'] or item['name'] != file['object_name']:
            raise HTTPException(409, 'OneDrive file changed; refusing unsafe resume')
        return RedirectResponse(item['download_url'], status_code=302,
                                headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})

    def progress(self, task_id, data):
        row = self.job(task_id, True)
        if row['status'] == 'completed':
            return {'ok': True}
        if row['status'] not in ('ready', 'downloading'):
            raise HTTPException(409, 'Transfer is not ready for the node')
        status, file_id = data.get('status'), data.get('file_id')
        if status not in ('downloading', 'completed', 'failed'):
            raise HTTPException(400, 'Invalid receive status')
        if status == 'failed':
            # Never persist raw URLs, filesystem errors or command output from a node.
            reason = data.get('error', 'receive_failed')
            if not isinstance(reason, str) or not re.fullmatch('[a-z_]{1,64}', reason):
                reason = 'receive_failed'
            self.db.execute("UPDATE relay_jobs SET status='failed',error=?,cleanup_pending=1,updated=? WHERE id=?",
                            (reason, time.time(), task_id))
        elif file_id is None:
            if status == 'downloading':
                self.db.execute("UPDATE relay_jobs SET status='downloading',updated=? WHERE id=?", (time.time(), task_id))
            else:
                if self.db.execute("SELECT 1 FROM relay_files WHERE job_id=? AND status!='completed' LIMIT 1", (task_id,)).fetchone():
                    raise HTTPException(409, 'Files are not yet confirmed on the node')
                self.db.execute("UPDATE relay_jobs SET status='completed',cleanup_pending=1,updated=? WHERE id=?", (time.time(), task_id))
        else:
            file = self.file(task_id, file_id)
            done = data.get('bytes_done')
            if type(done) is not int or not 0 <= done <= file['size'] or status == 'completed' and done != file['size']:
                raise HTTPException(400, 'Invalid receive progress')
            if file['status'] != 'completed':
                self.db.execute('UPDATE relay_files SET downloaded=MAX(downloaded,?),status=? WHERE id=?', (done, status, file_id))
            self.db.execute("UPDATE relay_jobs SET status='downloading',updated=? WHERE id=?", (time.time(), task_id))
        self.db.commit()
        return {'ok': True}

    def cancel(self, task_id):
        row = self.job(task_id)
        if row['status'] not in TERMINAL:
            self.db.execute("UPDATE relay_jobs SET status='cancelled',cleanup_pending=1,updated=? WHERE id=?", (time.time(), task_id))
            self.db.commit()
        return self.public(self.job(task_id), True)

    async def cleanup_once(self):
        now = time.time()
        self.db.execute("UPDATE relay_jobs SET status='expired',cleanup_pending=1,updated=? WHERE expires<=? "
                        "AND status IN ('uploading','ready','downloading')", (now, now))
        self.db.execute("UPDATE relay_jobs SET status='cancelled',cleanup_pending=1,updated=? WHERE node_id NOT IN "
                        "(SELECT id FROM nodes WHERE active=1) AND status IN ('uploading','ready','downloading')", (now,))
        self.db.commit()
        for job in self.db.execute('SELECT id FROM relay_jobs WHERE cleanup_pending=1 AND cleanup_after<=? '
                                   'ORDER BY cleanup_after,updated LIMIT 8', (now,)).fetchall():
            task_id = job['id']
            async with self.lock(task_id):
                # Rotate before network I/O, including timeout/cancellation, so a
                # few permanently blocked objects cannot starve all later cleanup.
                attempted = time.time()
                self.db.execute('UPDATE relay_jobs SET cleanup_after=?,cleanup_attempted=? WHERE id=?',
                                (attempted+60, attempted, task_id))
                self.db.commit()
                if self.job(task_id)['storage_binding'] != self.storage_binding():
                    self.db.execute("UPDATE relay_jobs SET cleanup_error='account_changed' WHERE id=?", (task_id,))
                    self.db.commit()
                    continue
                for file in self.db.execute('SELECT * FROM relay_files WHERE job_id=? AND cleaned=0 AND cleanup_after<=? '
                                            'ORDER BY cleanup_after,rowid LIMIT 32', (task_id, time.time())).fetchall():
                    # Rotate within a large task too: changed or inaccessible
                    # objects must not pin its first page forever.
                    self.db.execute('UPDATE relay_files SET cleanup_after=? WHERE id=?', (time.time()+60, file['id']))
                    self.db.commit()
                    try:
                        if file['item_id']:
                            await self.call('permanent_delete', file['item_id'],
                                            expected_name=file['object_name'], expected_etag=file['etag'])
                        else:
                            if file['upload_url']:
                                try:
                                    await self.call('cancel_upload', file['upload_url'])
                                except HTTPException as exc:
                                    if exc.status_code not in (404, 410):
                                        raise
                            # A missing session may mean final PUT already committed.
                            # Also reconcile zero-byte creation with a lost response.
                            item = await self.find(file)
                            if item:
                                # Persist identity before delete so retries cannot bind a replacement.
                                self.db.execute('UPDATE relay_files SET item_id=?,etag=? WHERE id=?',
                                                (item['id'], item['etag'], file['id']))
                                self.db.commit()
                                await self.call('permanent_delete', item['id'],
                                                expected_name=file['object_name'], expected_etag=item['etag'])
                    except HTTPException as exc:
                        # A missing bound item can be in the recycle bin. It is not
                        # proof of a successful permanent deletion or released quota.
                        code = ('object_changed' if exc.status_code in (409, 412) else
                                'object_unavailable' if exc.status_code in (404, 410) else
                                'authorization_required' if exc.status_code == 401 or 'authorization_required' in str(exc.detail) else
                                'permission_denied' if exc.status_code == 403 else
                                'permanent_delete_unavailable' if 'permanent_delete_unsupported' in str(exc.detail) else
                                'provider_unavailable')
                        self.db.execute('UPDATE relay_jobs SET cleanup_error=? WHERE id=?', (code, task_id))
                        self.db.commit()
                        continue
                    self.db.execute('UPDATE relay_files SET cleaned=1,upload_url=NULL WHERE id=?', (file['id'],))
                    self.db.commit()
                if not self.db.execute('SELECT 1 FROM relay_files WHERE job_id=? AND cleaned=0 LIMIT 1', (task_id,)).fetchone():
                    self.db.execute("UPDATE relay_jobs SET cleanup_pending=0,cleanup_error='',cleanup_completed=? WHERE id=?",
                                    (time.time(), task_id))
                    self.db.commit()

    async def maintenance(self):
        while True:
            try:
                async with asyncio.timeout(45):
                    await self.cleanup_once()
            except Exception:
                # Stored cleanup intent survives process/provider outages. No URLs in logs.
                self.db.rollback()
            await asyncio.sleep(30)

    async def close(self):
        await self.provider.close()


def install_routes(app, relay, admin, node_auth):
    @app.post('/api/relay/transfers')
    async def create(request: Request):
        admin(request)
        return await relay.create(await body(request))

    @app.get('/api/relay/transfers/{task_id}')
    async def get(task_id: str, request: Request):
        admin(request)
        return relay.public(relay.job(task_id), True)

    @app.post('/api/relay/transfers/{task_id}/files/{file_id}/session')
    async def session(task_id: str, file_id: str, request: Request):
        admin(request)
        return await relay.session(task_id, file_id)

    @app.post('/api/relay/transfers/{task_id}/files/{file_id}/complete')
    async def complete(task_id: str, file_id: str, request: Request):
        admin(request)
        return await relay.complete_file(task_id, file_id)

    @app.post('/api/relay/transfers/{task_id}/files/{file_id}/upload-progress')
    async def upload_progress(task_id: str, file_id: str, request: Request):
        admin(request)
        return relay.upload_progress(task_id, file_id, await body(request))

    @app.post('/api/relay/transfers/{task_id}/ready')
    async def ready(task_id: str, request: Request):
        admin(request)
        return relay.ready(task_id)

    @app.post('/api/relay/transfers/{task_id}/cancel')
    async def cancel(task_id: str, request: Request):
        admin(request)
        return relay.cancel(task_id)

    @app.get('/api/nodes/{node_id}/relay-task')
    async def next_task(node_id: str, request: Request):
        node_auth(request, node_id)
        return relay.next_task(node_id)

    @app.get('/api/relay/transfers/{task_id}/files/{file_id}/content')
    async def content(task_id: str, file_id: str, request: Request):
        # An administrator can use the same stable download URL. Other nodes cannot.
        job = relay.job(task_id, True)
        try:
            admin(request)
        except HTTPException:
            node_auth(request, job['node_id'])
        return await relay.content(task_id, file_id)

    @app.post('/api/nodes/{node_id}/relay-tasks/{task_id}/progress')
    async def progress(node_id: str, task_id: str, request: Request):
        node_auth(request, node_id)
        if relay.job(task_id)['node_id'] != node_id:
            raise HTTPException(403, 'Transfer belongs to another node')
        return relay.progress(task_id, await body(request))
