import time

PROCESS_STARTED = time.monotonic()

import asyncio, contextlib, hashlib, hmac, json, logging, math, os, re, secrets, shlex, sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta
import asyncssh, httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fleet_oauth import install_oauth
from probe import ProbeStore
from relay_transfer import RelayTransfers, install_routes as install_relay_routes
from static.node import NetworkSampler
from onedrive import OneDrive
from onedrive_auth import OneDriveAuth, recover_pending, install_routes as install_onedrive_auth_routes

ROOT = Path(os.environ.get('FLEET_ROOT', '/var/lib/fleet'))
CODE = Path(__file__).parent
recover_pending(ROOT)
CFG = json.loads((ROOT / 'config.json').read_text())
BASE = CFG['base_url']
FRP_VERSION = '0.71.1'
TRANSFER_PREPARE_TIMEOUT = 40
TRANSFER_PHASES = {'preparing_tool', 'connecting', 'transferring', 'completed', 'failed'}
MONITOR_INTERVAL = 5
MONITOR_STALE_AFTER = 20

def ensure_transfer_fields(connection):
    # Additive migration: old clients and existing transfer history stay valid.
    columns = {row[1] for row in connection.execute('PRAGMA table_info(transfers)')}
    for name, kind in (('phase', 'TEXT'), ('elapsed_seconds', 'REAL'), ('transfer_seconds', 'REAL'),
                       ('progress_stale', 'INTEGER NOT NULL DEFAULT 0')):
        if name not in columns:
            connection.execute(f'ALTER TABLE transfers ADD COLUMN {name} {kind}')

db = sqlite3.connect(ROOT / 'fleet.db', check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript('''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY,name TEXT,user TEXT,port INTEGER UNIQUE,token_hash TEXT,host_key TEXT,secret TEXT,active INTEGER DEFAULT 1,last_seen REAL DEFAULT 0,metrics TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS enrollments(token_hash TEXT PRIMARY KEY,name TEXT,expires REAL);
CREATE TABLE IF NOT EXISTS transfers(id TEXT PRIMARY KEY,node_id TEXT,name TEXT,token_hash TEXT,expires REAL,status TEXT,bytes_total INTEGER,bytes_done INTEGER DEFAULT 0,speed_bps REAL DEFAULT 0,updated_at REAL,error TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS transfers_active_node ON transfers(node_id,status,expires);
''')
ensure_transfer_fields(db)
db.commit()
probes = ProbeStore(db)
relay_transfers = RelayTransfers(db, CFG, ROOT)

@contextlib.asynccontextmanager
async def onedrive_change_guard(settings):
    # Creation cannot race the final account check. A same-drive reauthorization
    # remains possible when expired credentials have left cleanup pending.
    previous = relay_transfers.provider
    async with relay_transfers.create_lock:
        previous = relay_transfers.provider
        async with previous._refresh_lock:
            if (relay_transfers.account_busy() and
                    relay_transfers.storage_binding(settings) != relay_transfers.storage_binding()):
                raise HTTPException(409, 'account_change_busy')
            yield
    if previous is not relay_transfers.provider:
        await previous.close()

def activate_onedrive(settings):
    replacement = OneDrive(settings, ROOT / 'onedrive-token.json')
    previous = relay_transfers.provider
    CFG['onedrive'] = dict(settings)
    relay_transfers.config = CFG['onedrive']
    relay_transfers.provider = replacement
    previous.retire()

onedrive_auth = OneDriveAuth(CFG, ROOT, get_provider=lambda: relay_transfers.provider,
                            busy=relay_transfers.account_busy, change_guard=onedrive_change_guard,
                            on_configured=activate_onedrive, cleanup_status=relay_transfers.cleanup_summary)
proxy_status = {}
proxy_status_updated = None
startup_ms = None
monitor_state = dict(last_attempt_at=None, last_success_at=None, last_completed_at=None,
                     cycle_ms=None, schedule_lag_ms=None, error_code=None)
frp_observation = dict(checked_at=None, last_success_at=None, request_ms=None, error_code=None)
monitor_completed = None
_script_checksums = {}
transfer_preparations = {}
hub_network_sampler = NetworkSampler()

async def monitor_network():
    while True:
        # The same interface selection and counter reset rules as the nodes.
        # All sysfs/procfs reads stay off the request and heartbeat event loop.
        await asyncio.to_thread(hub_network_sampler.sample)
        await asyncio.sleep(MONITOR_INTERVAL)

def script_checksum(path):
    stat = path.stat()
    signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    cached = _script_checksums.get(path)
    if cached is None or cached[0] != signature:
        cached = (signature, hashlib.sha256(path.read_bytes()).hexdigest())
        _script_checksums[path] = cached
    return cached[1]

def tunnel_status(node_id):
    if proxy_status_updated is None or time.monotonic() - proxy_status_updated >= MONITOR_STALE_AFTER:
        return None
    return proxy_status.get(node_id + '.ssh')

def process_memory():
    # Current resident memory of this HTTP process, not the whole host/MCP/FRP stack.
    try:
        return int(Path('/proc/self/statm').read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, IndexError, AttributeError):
        return None

def hub_diagnostics():
    now = time.monotonic()
    age = None if proxy_status_updated is None else max(0, now - proxy_status_updated)
    monitor_status = (('starting' if now - PROCESS_STARTED < MONITOR_STALE_AFTER else 'stale') if monitor_completed is None else
                      'stale' if now - monitor_completed >= MONITOR_STALE_AFTER else
                      'degraded' if monitor_state['error_code'] else 'ok')
    frp_status = ('error' if frp_observation['error_code'] else 'unknown' if age is None else
                  'stale' if age >= MONITOR_STALE_AFTER else 'ok')
    return dict(uptime_seconds=max(0, now - PROCESS_STARTED), startup_ms=startup_ms, process_rss_bytes=process_memory(),
                network=hub_network_sampler.snapshot(),
                monitor=dict(monitor_state, status=monitor_status),
                frp=dict(frp_observation, status=frp_status, snapshot_age_seconds=age))

def digest(token): return hashlib.sha256(token.encode()).hexdigest()
def bearer(request): return request.headers.get('authorization', '').removeprefix('Bearer ')
def admin(request):
    if not hmac.compare_digest(bearer(request), CFG['admin_token']): raise HTTPException(401, 'Authentication required')
def node_auth(request, node_id):
    row = db.execute('SELECT * FROM nodes WHERE id=? AND active=1', (node_id,)).fetchone()
    if not row or not hmac.compare_digest(digest(bearer(request)), row['token_hash']): raise HTTPException(401, 'Invalid node credential')
    return row
def atomic(path, text):
    path = Path(path); tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(text); tmp.chmod(0o600); tmp.replace(path)
def refresh_inventory():
    rows = db.execute('SELECT * FROM nodes WHERE active=1').fetchall()
    inv = {r['id']: {'hostname':'127.0.0.1','port':r['port'],'user':r['user'],'identity_file':str(ROOT/'keys'/r['id']),'known_hosts':'strict'} for r in rows}
    atomic(ROOT/'.ssh'/'known_hosts', ''.join(f"[127.0.0.1]:{r['port']} {r['host_key']}\n" for r in rows))
    atomic(ROOT/'inventory.json', json.dumps(inv))
def public_node(r):
    now = time.time()
    metrics = json.loads(r['metrics'])
    metrics.pop('latency_samples', None)
    frp_status = tunnel_status(r['id'])
    connection_status = ('offline' if now-r['last_seen'] >= 35 or frp_status == 'offline' else
                         'online' if frp_status == 'online' else 'unknown')
    node = dict(id=r['id'],name=r['name'],ssh_user=r['user'],last_seen=r['last_seen'],
                online=connection_status=='online', connection_status=connection_status,
                frp_status=frp_status,**metrics)
    return probes.enrich(node, now, manual_location=r['manual_location'])

def update_status():
    try:
        status = json.loads((ROOT / 'update-status.json').read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(status, dict):
        return {}
    fields = ('current_commit', 'target_commit', 'checked_at', 'deployed_at', 'last_success_at', 'status', 'message',
              'prepare_seconds', 'dependencies_reused')
    return {key: status[key] for key in fields if isinstance(status.get(key), (str, int, float))}

async def monitor():
    global proxy_status_updated, monitor_completed
    async with httpx.AsyncClient(timeout=4, trust_env=False) as client:
        scheduled = time.monotonic()
        while True:
            started = time.monotonic()
            monitor_state.update(last_attempt_at=time.time(), schedule_lag_ms=max(0, started-scheduled)*1000,
                                 error_code=None)
            try:
                try:
                    response = await client.get('http://127.0.0.1:7500/api/proxy/tcp', auth=('fleet',CFG['frp_dashboard_password']))
                    response.raise_for_status()
                    snapshot = {p['name']: p['status'] for p in response.json().get('proxies', [])}
                    proxy_status.clear()
                    proxy_status.update(snapshot)
                    proxy_status_updated = time.monotonic()
                    frp_observation.update(last_success_at=time.time(), error_code=None)
                except Exception as exc:
                    proxy_status.clear()
                    frp_observation['error_code'] = type(exc).__name__
                    monitor_state['error_code'] = 'frp_unavailable'
                finally:
                    frp_observation.update(checked_at=time.time(), request_ms=(time.monotonic()-started)*1000)
                probes.mark_offline([
                    {'id': r['id'], 'last_seen': r['last_seen'], 'frp_status': tunnel_status(r['id'])}
                    for r in db.execute('SELECT id,last_seen FROM nodes WHERE active=1')])
                # A missing progress report does not establish that the P2P data path failed.
                db.execute("UPDATE transfers SET progress_stale=1,speed_bps=0 WHERE status IN ('connecting','transferring') AND progress_stale=0 AND updated_at<?", (time.time()-90,))
                db.commit()
                if monitor_state['error_code'] is None:
                    monitor_state['last_success_at'] = time.time()
            except Exception as exc:
                db.rollback()
                monitor_state['error_code'] = type(exc).__name__
                # Exception messages may contain paths or credentials; retain only the class.
                logging.getLogger('uvicorn.error').warning('Fleet monitor iteration failed: %s', type(exc).__name__)
            monitor_completed = time.monotonic()
            monitor_state.update(last_completed_at=time.time(), cycle_ms=(monitor_completed-started)*1000)
            delay = max(.1, MONITOR_INTERVAL-(time.monotonic()-started))
            scheduled = time.monotonic() + delay
            await asyncio.sleep(delay)

@contextlib.asynccontextmanager
async def lifespan(app):
    global startup_ms
    refresh_inventory()
    task = asyncio.create_task(monitor())
    network_task = asyncio.create_task(monitor_network())
    relay_task = asyncio.create_task(relay_transfers.maintenance())
    startup_ms = (time.monotonic()-PROCESS_STARTED)*1000
    try:
        yield
    finally:
        task.cancel()
        network_task.cancel()
        relay_task.cancel()
        with contextlib.suppress(asyncio.CancelledError): await task
        with contextlib.suppress(asyncio.CancelledError): await network_task
        with contextlib.suppress(asyncio.CancelledError): await relay_task
        await onedrive_auth.close()
        await relay_transfers.close()

app = FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
install_oauth(app, CFG, ROOT)
install_relay_routes(app, relay_transfers, admin, node_auth)
install_onedrive_auth_routes(app, onedrive_auth, admin)

@app.get('/')
async def index(): return FileResponse(CODE/'static'/'index.html')
@app.get('/health')
async def health(): return {'ok':True}
@app.get('/install.sh')
async def installer():
    return PlainTextResponse((CODE/'static'/'install.sh').read_text(encoding='utf-8').replace('__HUB_URL__',BASE))
@app.get('/node.py')
async def agent_script(): return FileResponse(CODE/'static'/'node.py',media_type='text/x-python')
@app.get('/node-supervisor.py')
async def agent_supervisor(): return FileResponse(CODE/'static'/'node-supervisor.py',media_type='text/x-python')
@app.get('/api/agent-release')
async def agent_release():
    checksum=script_checksum(CODE/'static'/'node.py')
    return {'version':checksum[:12],'url':BASE+'/node.py','sha256':checksum,
            'supervisor':{'url':BASE+'/node-supervisor.py',
                          'sha256':script_checksum(CODE/'static'/'node-supervisor.py')}}
@app.get('/send.sh')
async def send_shell():
    return PlainTextResponse((CODE/'static'/'send.sh').read_text(encoding='utf-8').replace('__HUB_URL__',BASE))
@app.get('/send.ps1')
async def send_powershell():
    return PlainTextResponse((CODE/'static'/'send.ps1').read_text(encoding='utf-8').replace('__HUB_URL__',BASE))
@app.get('/transfer.py')
async def transfer_script(request:Request):
    path = CODE/'static'/'transfer.py'
    etag = '"' + script_checksum(path) + '"'
    headers = {'ETag': etag, 'Cache-Control': 'no-cache'}
    matches = [value.strip().removeprefix('W/') for value in request.headers.get('if-none-match', '').split(',')]
    if etag in matches or '*' in matches:
        return Response(status_code=304, headers=headers)
    return FileResponse(path,media_type='text/x-python',filename='transfer.py',headers=headers)
app.mount('/downloads',StaticFiles(directory=CODE/'downloads'),name='downloads')

@app.get('/api/state')
async def state(request:Request):
    admin(request)
    nodes = [public_node(r) for r in db.execute('SELECT n.*,p.manual_location FROM nodes n '
               'LEFT JOIN probe_metadata p ON p.node_id=n.id WHERE n.active=1 ORDER BY n.name')]
    transfers = [dict(r) for r in db.execute('SELECT id,node_id,name,status,phase,bytes_total,bytes_done,speed_bps,elapsed_seconds,transfer_seconds,updated_at,error,progress_stale,expires FROM transfers ORDER BY updated_at DESC LIMIT 50')]
    now = time.time()
    for t in transfers:
        t['mode'] = 'p2p'
        t['progress_stale'] = bool(t['progress_stale'])
        t['authorization_expired'] = t['expires'] is not None and t['expires'] <= now
    transfers = sorted(transfers + relay_transfers.recent(), key=lambda t: t['updated_at'] or 0, reverse=True)[:50]
    storage = onedrive_auth.snapshot()
    # Device codes belong only to the dedicated admin login UI, not MCP/status.
    storage.pop('login', None)
    return dict(nodes=nodes,transfers=transfers,mcp_url=app.state.oauth_provider.base+'/mcp',version='1.1.0',update=update_status(),hub=hub_diagnostics(),
                storage={'onedrive': dict(storage, enabled=relay_transfers.enabled)})

def active_node(node_id):
    if not db.execute('SELECT 1 FROM nodes WHERE id=? AND active=1', (node_id,)).fetchone():
        raise HTTPException(404, 'Node not found')

@app.get('/api/nodes/{node_id}/history')
async def probe_history(node_id:str, request:Request, metric:str='cpu', range:str='1h', device:str|None=None):
    admin(request); active_node(node_id)
    try: return probes.history(node_id, metric, range, device)
    except ValueError as exc: raise HTTPException(400, str(exc))

@app.get('/api/latency/summary')
async def probe_latency(request:Request, range:str='24h', bucket:str='15m', healthy_ms:float=100, critical_ms:float=250):
    admin(request)
    if range != '24h' or bucket != '15m': raise HTTPException(400, 'Supported window: 24h / 15m')
    ids = [r[0] for r in db.execute('SELECT id FROM nodes WHERE active=1')]
    try: return probes.cached_latency_summary(ids, healthy_ms, critical_ms)
    except ValueError as exc: raise HTTPException(400, str(exc))

@app.put('/api/nodes/{node_id}/location')
async def probe_location(node_id:str, request:Request):
    admin(request); active_node(node_id)
    try: return probes.set_location(node_id, await request.json())
    except ValueError as exc: raise HTTPException(400, str(exc))

@app.post('/api/enrollments')
async def enrollment(request:Request):
    admin(request); data = await request.json()
    name = str(data.get('name','training-server')).strip()[:80] or 'training-server'
    token=secrets.token_urlsafe(32); expires=time.time()+1800
    db.execute('INSERT INTO enrollments VALUES(?,?,?)',(digest(token),name,expires)); db.commit()
    return {'command':f"curl -fsSL {BASE}/install.sh | bash -s -- {shlex.quote(token)}",'expires_at':expires}

@app.post('/api/enroll')
async def enroll(request:Request):
    data=await request.json(); token_hash=digest(bearer(request))
    grant=db.execute('SELECT * FROM enrollments WHERE token_hash=? AND expires>?',(token_hash,time.time())).fetchone()
    if not grant: raise HTTPException(401,'Enrollment token expired or already used')
    if not isinstance(data,dict): raise HTTPException(400,'Expected an object')
    platform=data.get('platform','linux_amd64')
    asset=CFG['assets'].get(platform) if isinstance(platform,str) else None
    if not asset: raise HTTPException(400,'Unsupported platform')
    try: ssh_port=int(data.get('ssh_port',22))
    except (TypeError,ValueError): raise HTTPException(400,'Invalid SSH port')
    if not 1 <= ssh_port <= 65535: raise HTTPException(400,'Invalid SSH port')
    ca_pem=(ROOT/'ca.pem').read_text()
    user=str(data.get('ssh_user',''))
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.-]{0,63}',user): raise HTTPException(400,'Invalid SSH user')
    try: host_key=asyncssh.import_public_key(data['ssh_host_key']).export_public_key().decode().strip()
    except Exception: raise HTTPException(400,'Invalid SSH host key')
    node_id='node-'+secrets.token_hex(5); token=secrets.token_urlsafe(32); secret=secrets.token_urlsafe(32)
    used={r[0] for r in db.execute('SELECT port FROM nodes')}
    port=next((x for x in range(24000,25000) if x not in used),None)
    if port is None: raise HTTPException(409,'Node capacity reached')
    key=asyncssh.generate_private_key('ssh-ed25519'); atomic(ROOT/'keys'/node_id,key.export_private_key().decode())
    public_key=key.export_public_key().decode().strip()+' fleet-'+node_id
    with db:
        deleted=db.execute('DELETE FROM enrollments WHERE token_hash=?',(token_hash,)).rowcount
        if deleted != 1: raise HTTPException(401,'Enrollment token used')
        db.execute('INSERT INTO nodes(id,name,user,port,token_hash,host_key,secret) VALUES(?,?,?,?,?,?,?)',(node_id,grant['name'],user,port,digest(token),host_key,secret))
    refresh_inventory()
    frpc=f'''serverAddr = "{CFG['host']}"
serverPort = 7000
user = "{node_id}"
clientID = "{node_id}"
metadatas.token = "{token}"
transport.tls.enable = true
transport.tls.serverName = "{CFG['host']}"
transport.tls.trustedCaFile = "__CA_FILE__"
transport.heartbeatInterval = 10
transport.heartbeatTimeout = 30
transport.tcpMuxKeepaliveInterval = 10
loginFailExit = false
[[proxies]]
name = "ssh"
type = "tcp"
localIP = "127.0.0.1"
localPort = {ssh_port}
remotePort = {port}
healthCheck.type = "tcp"
healthCheck.timeoutSeconds = 3
healthCheck.maxFailed = 2
healthCheck.intervalSeconds = 10
[[proxies]]
name = "data"
type = "xtcp"
localIP = "127.0.0.1"
localPort = {ssh_port}
secretKey = "{secret}"
allowUsers = ["*"]
'''
    return dict(id=node_id,token=token,name=grant['name'],public_key=public_key,frpc_config=frpc,ca_pem=ca_pem,download_url=BASE+'/downloads/'+asset['name'],sha256=asset['sha256'])

@app.post('/api/nodes/{node_id}/heartbeat')
async def heartbeat(node_id:str,request:Request):
    node_auth(request,node_id); data=await request.json()
    if not isinstance(data, dict): raise HTTPException(400, 'Expected an object')
    now = time.time()
    metrics = probes.clean_metrics(data)
    record_resource_sample = True
    if 'metrics_status' in metrics or 'metrics_sampled_at' in metrics:
        sampled_at = metrics.get('metrics_sampled_at')
        previous = db.execute('SELECT metrics FROM nodes WHERE id=?', (node_id,)).fetchone()
        previous_sample = json.loads(previous['metrics']).get('metrics_sampled_at')
        record_resource_sample = (metrics.get('metrics_status') == 'ok' and sampled_at is not None
                                  and now-35 < sampled_at <= now+60 and sampled_at != previous_sample)
    latest = {k: v for k, v in metrics.items() if k != 'latency_samples'}
    db.execute('UPDATE nodes SET last_seen=?,metrics=? WHERE id=?',(now,json.dumps(latest),node_id))
    probes.record(node_id, metrics, now, record_resource_sample=record_resource_sample)
    active_transfer = db.execute("SELECT 1 FROM transfers WHERE node_id=? AND expires>? "
                                 "AND status IN ('connecting','transferring') LIMIT 1", (node_id, now)).fetchone()
    return {'ok':True, 'update_allowed': active_transfer is None and not transfer_preparations.get(node_id) and not relay_transfers.busy(node_id),
            'relay_available': relay_transfers.available(node_id)}

@app.post('/api/nodes/{node_id}/revoke')
async def revoke(node_id:str,request:Request):
    admin(request)
    db.execute('UPDATE nodes SET active=0 WHERE id=?',(node_id,)); db.commit(); refresh_inventory()
    # FRP NewUserConn and NewWorkConn hooks deny further tunneled connections.
    return {'ok':True}

@app.post('/internal/frp')
async def frp_hook(request:Request):
    if request.client.host not in ('127.0.0.1','::1'): raise HTTPException(403)
    data=await request.json(); op=data.get('op'); content=data.get('content',{})
    identity=content if op=='Login' else content.get('user',{})
    # FRP TOML uses metadatas, but the HTTP plugin wire payload uses metas.
    user=identity.get('user',''); token=identity.get('metas',{}).get('token','')
    if not isinstance(user,str) or not isinstance(token,str):
        return {'reject':True,'reject_reason':'Invalid credential format','unchange':True}
    node=db.execute('SELECT * FROM nodes WHERE id=? AND active=1',(user,)).fetchone()
    valid=bool(node and hmac.compare_digest(digest(token),node['token_hash'])) and op in ('Login','NewProxy','NewUserConn','NewWorkConn','Ping')
    transfer=db.execute('SELECT * FROM transfers WHERE id=? AND expires>? AND status IN (\'connecting\',\'transferring\')',(user,time.time())).fetchone()
    if transfer and hmac.compare_digest(digest(token),transfer['token_hash']):
        active=db.execute('SELECT 1 FROM nodes WHERE id=? AND active=1',(transfer['node_id'],)).fetchone()
        valid=bool(active) and op in ('Login','NewWorkConn','Ping')
    if valid and op=='Login':
        valid=content.get('client_id')==user
    if valid and op=='NewProxy':
        typ=content.get('proxy_type'); name=content.get('proxy_name')
        valid=bool(node and ((typ=='tcp' and name==user+'.ssh' and content.get('remote_port')==node['port']) or (typ=='xtcp' and name==user+'.data' and content.get('sk')==node['secret'] and content.get('allow_users')==['*'])))
    if valid and op=='NewUserConn':
        valid=bool(node and content.get('proxy_name')==user+'.ssh' and content.get('proxy_type')=='tcp')
    return {'reject':not valid,'reject_reason':'' if valid else 'Invalid or revoked node credential','unchange':True}

@app.post('/api/transfers')
async def create_transfer(request:Request):
    admin(request); data=await request.json()
    node=db.execute('SELECT * FROM nodes WHERE id=? AND active=1',(data.get('node_id'),)).fetchone()
    if not node: raise HTTPException(404,'Node not found')
    try: pub=asyncssh.import_public_key(data['public_key']).export_public_key().decode().strip()
    except Exception: raise HTTPException(400,'Invalid public key')
    platform=data.get('platform','linux_amd64'); asset=CFG['assets'].get(platform)
    if not asset: raise HTTPException(400,'Unsupported platform')
    transfer_id='transfer-'+secrets.token_hex(6); token=secrets.token_urlsafe(32); expires=time.time()+86400
    stage = 'ssh_connect'
    transfer_preparations[node['id']] = transfer_preparations.get(node['id'], 0) + 1
    try:
        async with asyncio.timeout(TRANSFER_PREPARE_TIMEOUT):
            async with asyncssh.connect('127.0.0.1',port=node['port'],username=node['user'],client_keys=[str(ROOT/'keys'/node['id'])],known_hosts=str(ROOT/'.ssh'/'known_hosts'),connect_timeout=12) as conn:
                # OpenSSH before 9.1 accepts local time only, without a UTC "Z" suffix.
                stage = 'target_clock'
                stamp=await conn.run(f'date -d @{int(expires)} +%Y%m%d%H%M%S',check=True,timeout=10)
                expiry=str(stamp.stdout).strip()
                if not re.fullmatch(r'\d{14}',expiry): raise ValueError('Invalid target clock response')
                line=f'restrict,expiry-time="{expiry}" {pub} fleet-{transfer_id}'
                command='umask 077; mkdir -p "$HOME/.ssh"; touch "$HOME/.ssh/authorized_keys"; chmod 700 "$HOME/.ssh"; chmod 600 "$HOME/.ssh/authorized_keys"; printf "\\n%s\\n" '+shlex.quote(line)+' >> "$HOME/.ssh/authorized_keys"'
                stage = 'authorize_key'
                await conn.run(command,check=True,timeout=15)
    except TimeoutError: raise HTTPException(504,'SSH enrollment timed out during '+stage) from None
    except Exception as e: raise HTTPException(502,'SSH enrollment failed during '+stage+': '+str(e))
    finally:
        remaining = transfer_preparations[node['id']] - 1
        if remaining:
            transfer_preparations[node['id']] = remaining
        else:
            transfer_preparations.pop(node['id'], None)
    db.execute('INSERT INTO transfers(id,node_id,name,token_hash,expires,status,phase,bytes_total,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',(transfer_id,node['id'],str(data.get('name','upload'))[:160],digest(token),expires,'connecting','preparing_tool',max(0,int(data.get('bytes_total',0))),time.time())); db.commit()
    frpc=f'''serverAddr = "{CFG['host']}"
serverPort = 7000
user = "{transfer_id}"
clientID = "{transfer_id}"
metadatas.token = "{token}"
transport.tls.enable = true
transport.tls.serverName = "{CFG['host']}"
transport.tls.trustedCaFile = "__CA_FILE__"
transport.heartbeatInterval = 10
transport.heartbeatTimeout = 30
transport.tcpMuxKeepaliveInterval = 10
loginFailExit = false
[[visitors]]
name = "data-visitor"
type = "xtcp"
serverUser = "{node['id']}"
serverName = "data"
secretKey = "{node['secret']}"
bindAddr = "127.0.0.1"
bindPort = 0
keepTunnelOpen = false
'''
    return dict(id=transfer_id,token=token,node_id=node['id'],ssh_user=node['user'],ssh_host_key=node['host_key'],frpc_config=frpc,ca_pem=(ROOT/'ca.pem').read_text(),frp_version=FRP_VERSION,download_url=BASE+'/downloads/'+asset['name'],sha256=asset['sha256'])

@app.post('/api/transfers/{transfer_id}/progress')
async def progress(transfer_id:str,request:Request):
    row=db.execute('SELECT * FROM transfers WHERE id=? AND expires>?',(transfer_id,time.time())).fetchone()
    if not row or not hmac.compare_digest(digest(bearer(request)),row['token_hash']): raise HTTPException(401)
    data=await request.json()
    if not isinstance(data, dict): raise HTTPException(400, 'Expected an object')
    status=data.get('status','transferring')
    if status not in ('connecting','transferring','completed','failed'): raise HTTPException(400)
    phase = data.get('phase', status)
    if (not isinstance(phase, str) or phase not in TRANSFER_PHASES or
            status == 'connecting' and phase not in ('preparing_tool', 'connecting') or
            status != 'connecting' and phase != status):
        raise HTTPException(400, 'Invalid transfer phase')
    timers = []
    for name in ('elapsed_seconds', 'transfer_seconds'):
        value = data.get(name)
        try:
            valid = value is None or type(value) in (int, float) and math.isfinite(value) and value >= 0
        except OverflowError:
            valid = False
        if not valid:
            raise HTTPException(400, 'Invalid '+name)
        timers.append(value)
    if (timers[0] is None) != (timers[1] is None):
        raise HTTPException(400, 'Elapsed and transfer times must be reported together')
    if all(value is not None for value in timers) and timers[1] > timers[0]:
        raise HTTPException(400, 'Transfer time exceeds elapsed time')
    try:
        done = int(data.get('bytes_done', row['bytes_done'] or 0))
        speed = float(data.get('speed_bps', row['speed_bps'] or 0))
    except (TypeError, ValueError, OverflowError):
        raise HTTPException(400, 'Invalid transfer progress') from None
    if not math.isfinite(speed): raise HTTPException(400, 'Invalid transfer speed')
    values = dict(id=transfer_id, status=status, phase=phase,
                  bytes_done=min(row['bytes_total'], max(0, done)), speed_bps=max(0, speed),
                  elapsed_seconds=timers[0], transfer_seconds=timers[1],
                  updated_at=time.time(), error=str(data.get('error', ''))[:1000])
    # Keep ordering guards in SQL: another request may finish while this one awaits its body.
    db.execute('''UPDATE transfers SET status=:status,phase=:phase,
        bytes_done=MAX(COALESCE(bytes_done,0),:bytes_done),speed_bps=:speed_bps,
        elapsed_seconds=CASE WHEN :elapsed_seconds IS NULL THEN elapsed_seconds
            ELSE MAX(COALESCE(elapsed_seconds,0),:elapsed_seconds) END,
        transfer_seconds=CASE WHEN :transfer_seconds IS NULL THEN transfer_seconds
            ELSE MAX(COALESCE(transfer_seconds,0),:transfer_seconds) END,
        updated_at=:updated_at,error=:error,progress_stale=0
        WHERE id=:id AND status IN ('connecting','transferring')
          AND NOT (status='transferring' AND :status='connecting')
          AND NOT (COALESCE(phase,'')='connecting' AND :phase='preparing_tool')
          AND (:status IN ('completed','failed') OR
               (:bytes_done>=COALESCE(bytes_done,0) AND
                (:elapsed_seconds IS NULL OR elapsed_seconds IS NULL OR :elapsed_seconds>=elapsed_seconds)))
        ''', values)
    db.commit()
    return {'ok':True}
