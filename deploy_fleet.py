import json,os,pathlib,secrets,subprocess,pwd
P=pathlib.Path
root=P('/var/lib/fleet'); code=P('/opt/fleet/app'); host='1-95-165-162.sslip.io'; endpoint='1.95.165.162'
try: pwd.getpwnam('fleet')
except KeyError: subprocess.run(['useradd','--system','--home-dir',str(root),'--shell','/usr/sbin/nologin','fleet'],check=True)
for p in [root,root/'keys',root/'.ssh',root/'logs',root/'caddy']:
    p.mkdir(parents=True,exist_ok=True); p.chmod(0o700)
cfgpath=root/'config.json'
if cfgpath.exists(): cfg=json.loads(cfgpath.read_text())
else: cfg={'host':host,'base_url':'https://'+endpoint,'admin_token':secrets.token_urlsafe(40),'frp_dashboard_password':secrets.token_urlsafe(32)}
cfg['base_url']='https://'+endpoint
cfg['assets']=json.loads((code/'assets.json').read_text()); cfgpath.write_text(json.dumps(cfg)); cfgpath.chmod(0o600)
if not (root/'ca.pem').exists():
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(root/'ca.key'),'-out',str(root/'ca.pem'),'-days','3650','-subj','/CN=Fleet FRP CA'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    subprocess.run(['openssl','req','-newkey','rsa:2048','-nodes','-keyout',str(root/'frps.key'),'-out',str(root/'frps.csr'),'-subj','/CN='+host],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    (root/'cert.ext').write_text('subjectAltName=DNS:'+host+'\nextendedKeyUsage=serverAuth\n')
    subprocess.run(['openssl','x509','-req','-in',str(root/'frps.csr'),'-CA',str(root/'ca.pem'),'-CAkey',str(root/'ca.key'),'-CAcreateserial','-out',str(root/'frps.pem'),'-days','3650','-extfile',str(root/'cert.ext')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
for p in root.glob('*.key'): p.chmod(0o600)
(root/'ssh_config').touch()
if not (root/'inventory.json').exists(): (root/'inventory.json').write_text('{}')
frps=f'''bindAddr = "0.0.0.0"
bindPort = 7000
proxyBindAddr = "127.0.0.1"
transport.tls.force = true
transport.tls.certFile = "{root}/frps.pem"
transport.tls.keyFile = "{root}/frps.key"
transport.heartbeatTimeout = 30
transport.tcpMuxKeepaliveInterval = 10
webServer.addr = "127.0.0.1"
webServer.port = 7500
webServer.user = "fleet"
webServer.password = "{cfg['frp_dashboard_password']}"
allowPorts = [{{start=24000,end=24999}}]
[[httpPlugins]]
name = "fleet-auth"
addr = "127.0.0.1:8090"
path = "/internal/frp"
ops = ["Login","NewProxy","NewWorkConn","NewUserConn","Ping"]
'''
(root/'frps.toml').write_text(frps); (root/'frps.toml').chmod(0o600)
caddy=f'''{{
    admin 127.0.0.1:2019
    default_sni {endpoint}
}}
{endpoint} {{
    tls {{
        issuer acme {{
            disable_tlsalpn_challenge
            profile shortlived
        }}
    }}
    encode zstd gzip
    header {{
        X-Content-Type-Options nosniff
        Referrer-Policy same-origin
        X-Frame-Options DENY
    }}
    handle /internal/* {{
        respond 404
    }}
    handle /mcp* {{
        forward_auth 127.0.0.1:8090 {{
            uri /internal/mcp-auth
        }}
        reverse_proxy 127.0.0.1:8080 {{
            header_up Host {{upstream_hostport}}
        }}
    }}
    handle {{
        reverse_proxy 127.0.0.1:8090
    }}
}}
'''
(root/'Caddyfile').write_text(caddy); (root/'Caddyfile').chmod(0o600)
relayenv='''RELAY_SHELL_TRANSPORT=http
RELAY_SHELL_HTTP_HOST=127.0.0.1
RELAY_SHELL_HTTP_PORT=8080
RELAY_SHELL_AUTH_ENABLED=false
RELAY_SHELL_POLICY_MODE=open
RELAY_SHELL_CONFIRM_TIER3=false
RELAY_SHELL_POLICY_DENY=
RELAY_SHELL_UNSAFE_NOTICES=false
RELAY_SHELL_INVENTORY=/var/lib/fleet/inventory.json
RELAY_SHELL_SSH_CONFIG=/var/lib/fleet/ssh_config
RELAY_SHELL_SSH_KNOWN_HOSTS=strict
RELAY_SHELL_AUDIT_PATH=/var/lib/fleet/logs/relay-audit.jsonl
RELAY_SHELL_MAX_CONNS=32
RELAY_SHELL_MAX_SESSIONS=16
RELAY_SHELL_SSH_KEEPALIVE=15
RELAY_SHELL_MAX_TIMEOUT=3600
'''
(root/'relay.env').write_text(relayenv)
services={
'fleet-hub':('Fleet node registry and probe dashboard','/opt/fleet/venv/bin/uvicorn app:app --app-dir /opt/fleet/app --host 127.0.0.1 --port 8090 --no-access-log',''),
'fleet-mcp':('Fleet SSH MCP (relay-shell)','/opt/fleet/venv/bin/python /opt/fleet/app/mcp_entry.py','EnvironmentFile=/var/lib/fleet/relay.env\n'),
'fleet-frps':('Fleet reverse tunnel server','/opt/fleet/bin/frps -c /var/lib/fleet/frps.toml','After=fleet-hub.service\n'),
'fleet-caddy':('Fleet HTTPS endpoint','/opt/fleet/bin/caddy run --config /var/lib/fleet/Caddyfile --adapter caddyfile','AmbientCapabilities=CAP_NET_BIND_SERVICE\nCapabilityBoundingSet=CAP_NET_BIND_SERVICE\nEnvironment=XDG_DATA_HOME=/var/lib/fleet/caddy\nEnvironment=GODEBUG=tlsmlkem=0\n'),
}
for name,(description,cmd,extra) in services.items():
    # Unit dependencies belong before [Service].
    dependency='After=fleet-hub.service\n' if name=='fleet-frps' else ''
    extra=extra.replace('After=fleet-hub.service\n','')
    text=f'[Unit]\nDescription={description}\nAfter=network-online.target\n{dependency}Wants=network-online.target\n[Service]\nUser=fleet\nGroup=fleet\nEnvironment=HOME=/var/lib/fleet\nEnvironment=FLEET_ROOT=/var/lib/fleet\nWorkingDirectory=/opt/fleet/app\nExecStart={cmd}\nRestart=always\nRestartSec=3\nUMask=0077\n{extra}[Install]\nWantedBy=multi-user.target\n'
    P('/etc/systemd/system/'+name+'.service').write_text(text)
subprocess.run(['chown','-R','fleet:fleet',str(root)],check=True)
subprocess.run(['/opt/fleet/bin/frps','verify','-c',str(root/'frps.toml')],check=True)
subprocess.run(['/opt/fleet/bin/caddy','validate','--config',str(root/'Caddyfile'),'--adapter','caddyfile'],check=True)
subprocess.run(['systemctl','daemon-reload'],check=True)
for name in services: subprocess.run(['systemctl','enable','--now',name],check=True)
print('Fleet services installed; credentials retained in /var/lib/fleet/config.json')
