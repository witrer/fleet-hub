#!/usr/bin/env python3
import argparse, hashlib, io, json, os, pathlib, platform, pwd, shutil, signal, socket, subprocess, sys, tarfile, time, urllib.request

def request(hub,path,token,payload):
    req=urllib.request.Request(hub+path,data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with urllib.request.urlopen(req,timeout=20) as response: return json.load(response)

def install(args):
    root=pathlib.Path(os.environ.get('FLEET_NODE_DIR',str(pathlib.Path.home()/'.local/share/fleet-node')))
    pending=root/'enrollment-pending.json'
    if (root/'config.json').exists() and not pending.exists(): raise SystemExit('该用户已注册，配置位于 '+str(root))
    arch={'x86_64':'amd64','aarch64':'arm64'}.get(platform.machine())
    if platform.system()!='Linux' or not arch: raise SystemExit('目标节点支持 Linux amd64/arm64')
    ssh_port=int(os.environ.get('FLEET_SSH_PORT','22'))
    if not 1 <= ssh_port <= 65535: raise SystemExit('FLEET_SSH_PORT 必须为 1 到 65535')
    try: socket.create_connection(('127.0.0.1',ssh_port),3).close()
    except OSError:
        if os.geteuid()==0 and shutil.which('apt-get'):
            subprocess.run(['apt-get','update','-qq'],check=True)
            subprocess.run(['apt-get','install','-y','openssh-server'],check=True)
            subprocess.run(['ssh-keygen','-A'],check=True)
            pathlib.Path('/run/sshd').mkdir(exist_ok=True)
            if pathlib.Path('/run/systemd/system').exists(): subprocess.run(['systemctl','enable','--now','ssh'],check=True)
            else: subprocess.run(['/usr/sbin/sshd'],check=True)
        else: raise SystemExit('需要运行中的 SSH 服务，可用 FLEET_SSH_PORT 指定端口')
    try: socket.create_connection(('127.0.0.1',ssh_port),3).close()
    except OSError: raise SystemExit('SSH 服务尚未监听指定端口 '+str(ssh_port))
    hostkeys=list(pathlib.Path('/etc/ssh').glob('ssh_host_ed25519_key.pub')) or list(pathlib.Path('/etc/ssh').glob('ssh_host_*_key.pub'))
    if not hostkeys: raise SystemExit('找不到 SSH 主机公钥')
    hostkey=hostkeys[0].read_text().split()
    user=pwd.getpwuid(os.getuid()).pw_name
    root.mkdir(parents=True,exist_ok=True); root.chmod(0o700)
    if pending.exists():
        saved=json.loads(pending.read_text())
        if saved['hub']!=args.hub: raise SystemExit('存在另一中枢的未完成安装：'+str(pending))
        data=saved['enrollment']
    else:
        data=request(args.hub,'/api/enroll',args.token,{'ssh_user':user,'ssh_port':ssh_port,'ssh_host_key':' '.join(hostkey[:2]),'platform':'linux_'+arch})
        temporary=pending.with_suffix('.tmp')
        temporary.write_text(json.dumps({'hub':args.hub,'enrollment':data})); temporary.chmod(0o600); temporary.replace(pending)
    with urllib.request.urlopen(data['download_url'],timeout=120) as response: binary=response.read()
    if hashlib.sha256(binary).hexdigest()!=data['sha256']: raise SystemExit('frp 下载校验失败')
    with tarfile.open(fileobj=io.BytesIO(binary),mode='r:gz') as tar:
        member=next(m for m in tar.getmembers() if m.isfile() and m.name.endswith('/frpc'))
        (root/'frpc').write_bytes(tar.extractfile(member).read())
    (root/'frpc').chmod(0o700)
    (root/'ca.pem').write_text(data['ca_pem'])
    (root/'frpc.toml').write_text(data['frpc_config'].replace('__CA_FILE__',str(root/'ca.pem')))
    cfg={'hub':args.hub,'id':data['id'],'token':data['token'],'name':data['name']}
    (root/'config.json').write_text(json.dumps(cfg)); (root/'config.json').chmod(0o600); (root/'frpc.toml').chmod(0o600)
    ssh=pathlib.Path.home()/'.ssh'; ssh.mkdir(mode=0o700,exist_ok=True); ssh.chmod(0o700)
    authorized=ssh/'authorized_keys'
    if not authorized.exists() or data['public_key'] not in authorized.read_text().splitlines():
        with authorized.open('a') as out: out.write('\n'+data['public_key']+'\n')
    (ssh/'authorized_keys').chmod(0o600)
    if pathlib.Path(__file__).resolve()!=(root/'node.py').resolve(): shutil.copyfile(__file__,root/'node.py')
    if os.geteuid()==0 and pathlib.Path('/run/systemd/system').exists():
        unit='[Unit]\nDescription=Fleet node probe and FRP tunnel\nAfter=network-online.target\nWants=network-online.target\n[Service]\nExecStart='+sys.executable+' '+str(root/'node.py')+' run --root '+str(root)+'\nRestart=always\nRestartSec=5\n[Install]\nWantedBy=multi-user.target\n'
        pathlib.Path('/etc/systemd/system/fleet-node.service').write_text(unit)
        subprocess.run(['systemctl','daemon-reload'],check=True); subprocess.run(['systemctl','enable','--now','fleet-node'],check=True)
    else:
        log=(root/'node.log').open('ab')
        subprocess.Popen([sys.executable,str(root/'node.py'),'run','--root',str(root)],stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        print('当前环境无系统服务：探针已在后台运行；容器重建后重新注册。')
    pending.unlink()
    print('已注册：'+data['name']+' ('+data['id']+')\n面板：'+args.hub)

def cpu():
    values=list(map(int,pathlib.Path('/proc/stat').read_text().splitlines()[0].split()[1:])); return sum(values),values[3]+values[4]
def run(args):
    root=pathlib.Path(args.root); cfg=json.loads((root/'config.json').read_text()); previous=cpu(); child=None
    def stop(*_):
        if child: child.terminate()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
    while True:
        if child is None or child.poll() is not None: child=subprocess.Popen([str(root/'frpc'),'-c',str(root/'frpc.toml')],stdout=sys.stdout,stderr=sys.stderr)
        current=cpu(); delta=current[0]-previous[0]; usage=100*(1-(current[1]-previous[1])/delta) if delta else 0; previous=current
        mem={line.split(':')[0]:int(line.split()[1]) for line in pathlib.Path('/proc/meminfo').read_text().splitlines()}
        disk=shutil.disk_usage('/'); net=[line.split(':',1)[1].split() for line in pathlib.Path('/proc/net/dev').read_text().splitlines()[2:] if not line.strip().startswith('lo:')]
        payload={'cpu_percent':round(usage,1),'memory_percent':round(100*(1-mem['MemAvailable']/mem['MemTotal']),1),'disk_percent':round(disk.used/disk.total*100,1),'uptime':float(pathlib.Path('/proc/uptime').read_text().split()[0]),'platform':platform.system()+' '+platform.machine(),'load':os.getloadavg()[0],'network_rx':sum(int(n[0]) for n in net),'network_tx':sum(int(n[8]) for n in net)}
        try: request(cfg['hub'],'/api/nodes/'+cfg['id']+'/heartbeat',cfg['token'],payload)
        except Exception as e: print('heartbeat: '+str(e),flush=True)
        time.sleep(10)

if __name__=='__main__':
    parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('install'); p.add_argument('--hub',required=True); p.add_argument('--token',required=True)
    p=sub.add_parser('run'); p.add_argument('--root',required=True)
    args=parser.parse_args(); install(args) if args.action=='install' else run(args)
