import asyncio, contextlib, hashlib, hmac, json, os, re, secrets, shlex, sqlite3, time
from pathlib import Path
from datetime import datetime, timezone, timedelta
import asyncssh, httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(os.environ.get('FLEET_ROOT', '/var/lib/fleet'))
CODE = Path(__file__).parent
CFG = json.loads((ROOT / 'config.json').read_text())
BASE = CFG['base_url']
FRP_VERSION = '0.71.1'
db = sqlite3.connect(ROOT / 'fleet.db', check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript('''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY,name TEXT,user TEXT,port INTEGER UNIQUE,token_hash TEXT,host_key TEXT,secret TEXT,active INTEGER DEFAULT 1,last_seen REAL DEFAULT 0,metrics TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS enrollments(token_hash TEXT PRIMARY KEY,name TEXT,expires REAL);
CREATE TABLE IF NOT EXISTS transfers(id TEXT PRIMARY KEY,node_id TEXT,name TEXT,token_hash TEXT,expires REAL,status TEXT,bytes_total INTEGER,bytes_done INTEGER DEFAULT 0,speed_bps REAL DEFAULT 0,updated_at REAL,error TEXT DEFAULT '');
''')
db.commit()
proxy_status = {}

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
    metrics = json.loads(r['metrics'])
    return dict(id=r['id'],name=r['name'],ssh_user=r['user'],last_seen=r['last_seen'],online=time.time()-r['last_seen']<35 and proxy_status.get(r['id']+'.ssh')=='online',**metrics)

def update_status():
    try:
        status = json.loads((ROOT / 'update-status.json').read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(status, dict):
        return {}
    fields = ('current_commit', 'target_commit', 'checked_at', 'deployed_at', 'last_success_at', 'status', 'message')
    return {key: status[key] for key in fields if isinstance(status.get(key), (str, int, float))}

async def monitor():
    async with httpx.AsyncClient(timeout=4) as client:
        while True:
            try:
                response = await client.get('http://127.0.0.1:7500/api/proxy/tcp', auth=('fleet',CFG['frp_dashboard_password']))
                response.raise_for_status()
                proxy_status.clear()
                proxy_status.update({p['name']:p['status'] for p in response.json().get('proxies',[])})
            except Exception: proxy_status.clear()
            db.execute("UPDATE transfers SET status='failed',error='客户端心跳中断',speed_bps=0 WHERE status IN ('connecting','transferring') AND updated_at<?", (time.time()-90,))
            db.commit()
            await asyncio.sleep(5)

@contextlib.asynccontextmanager
async def lifespan(app):
    refresh_inventory()
    task = asyncio.create_task(monitor())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError): await task

app = FastAPI(lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)

@app.get('/')
async def index(): return FileResponse(CODE/'static'/'index.html')
@app.get('/health')
async def health(): return {'ok':True}
@app.get('/install.sh')
async def installer():
    return PlainTextResponse((CODE/'static'/'install.sh').read_text().replace('__HUB_URL__',BASE))
@app.get('/node.py')
async def agent_script(): return FileResponse(CODE/'static'/'node.py',media_type='text/x-python')
@app.get('/transfer.py')
async def transfer_script(): return FileResponse(CODE/'static'/'transfer.py',media_type='text/x-python',filename='transfer.py')
app.mount('/downloads',StaticFiles(directory=CODE/'downloads'),name='downloads')

@app.get('/api/state')
async def state(request:Request):
    admin(request)
    nodes = [public_node(r) for r in db.execute('SELECT * FROM nodes WHERE active=1 ORDER BY name')]
    transfers = [dict(r) for r in db.execute('SELECT id,node_id,name,status,bytes_total,bytes_done,speed_bps,updated_at,error FROM transfers ORDER BY updated_at DESC LIMIT 50')]
    for t in transfers: t['mode']='p2p'
    return dict(nodes=nodes,transfers=transfers,mcp_url=BASE+'/mcp',version='1.0.0',update=update_status())

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
    metrics={k:data[k] for k in ('cpu_percent','memory_percent','disk_percent','uptime','platform','load','network_rx','network_tx') if k in data}
    db.execute('UPDATE nodes SET last_seen=?,metrics=? WHERE id=?',(time.time(),json.dumps(metrics),node_id)); db.commit()
    return {'ok':True}

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
    try:
        async with asyncssh.connect('127.0.0.1',port=node['port'],username=node['user'],client_keys=[str(ROOT/'keys'/node['id'])],known_hosts=str(ROOT/'.ssh'/'known_hosts'),connect_timeout=12) as conn:
            # OpenSSH before 9.1 accepts local time only, without a UTC "Z" suffix.
            stamp=await conn.run(f'date -d @{int(expires)} +%Y%m%d%H%M%S',check=True,timeout=10)
            expiry=str(stamp.stdout).strip()
            if not re.fullmatch(r'\d{14}',expiry): raise ValueError('Invalid target clock response')
            line=f'restrict,expiry-time="{expiry}" {pub} fleet-{transfer_id}'
            command='umask 077; mkdir -p "$HOME/.ssh"; touch "$HOME/.ssh/authorized_keys"; chmod 700 "$HOME/.ssh"; chmod 600 "$HOME/.ssh/authorized_keys"; printf "\\n%s\\n" '+shlex.quote(line)+' >> "$HOME/.ssh/authorized_keys"'
            await conn.run(command,check=True,timeout=15)
    except Exception as e: raise HTTPException(502,'SSH enrollment failed: '+str(e))
    db.execute('INSERT INTO transfers(id,node_id,name,token_hash,expires,status,bytes_total,updated_at) VALUES(?,?,?,?,?,?,?,?)',(transfer_id,node['id'],str(data.get('name','upload'))[:160],digest(token),expires,'connecting',max(0,int(data.get('bytes_total',0))),time.time())); db.commit()
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
    data=await request.json(); status=data.get('status','transferring')
    if status not in ('connecting','transferring','completed','failed'): raise HTTPException(400)
    db.execute('UPDATE transfers SET status=?,bytes_done=?,speed_bps=?,updated_at=?,error=? WHERE id=?',(status,min(row['bytes_total'],max(0,int(data.get('bytes_done',0)))),max(0,float(data.get('speed_bps',0))),time.time(),str(data.get('error',''))[:1000],transfer_id)); db.commit()
    return {'ok':True}
