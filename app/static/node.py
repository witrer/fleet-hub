#!/usr/bin/env python3
"""Fleet Linux node: enrollment, persistent tunnel, metrics and updates."""
import argparse
import contextlib
import ctypes
import fcntl
import hashlib
import io
import json
import os
import pathlib
import platform
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
import urllib.parse
import urllib.request

UPDATE_INTERVAL = 600
RESTART_DELAY = 2


def request(hub, path, token='', payload=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(hub + path, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def atomic(path, content, mode=0o600):
    path = pathlib.Path(path)
    temp = path.with_name(path.name + '.tmp')
    temp.write_bytes(content.encode() if isinstance(content, str) else content)
    temp.chmod(mode)
    temp.replace(path)


@contextlib.contextmanager
def lock(root, name):
    with (root / (name + '.lock')).open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Fleet ' + name + ' 已在运行')
        yield


def listening(port):
    try:
        socket.create_connection(('127.0.0.1', port), 3).close()
        return True
    except OSError:
        return False


def privileged():
    if os.geteuid() == 0:
        return []
    if shutil.which('sudo') and subprocess.run(['sudo', '-n', 'true'], stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL).returncode == 0:
        return ['sudo', '-n']
    raise SystemExit('此节点未运行 SSH，且当前用户无安装权限。请管理员安装并启动 openssh-server 后重跑本命令。')


def ensure_ssh(port):
    if listening(port):
        return
    prefix = privileged()
    if not (shutil.which('sshd') or pathlib.Path('/usr/sbin/sshd').exists()):
        commands = None
        if shutil.which('apt-get'):
            commands = [['apt-get', 'update', '-qq'], ['apt-get', 'install', '-y', 'openssh-server']]
        elif shutil.which('dnf'):
            commands = [['dnf', 'install', '-y', 'openssh-server']]
        elif shutil.which('yum'):
            commands = [['yum', 'install', '-y', 'openssh-server']]
        elif shutil.which('apk'):
            commands = [['apk', 'add', '--no-cache', 'openssh']]
        if not commands:
            raise SystemExit('未找到支持的包管理器：apt/dnf/yum/apk；请安装并启动 OpenSSH。')
        for command in commands:
            subprocess.run(prefix + command, env=dict(os.environ, DEBIAN_FRONTEND='noninteractive'), check=True)
    subprocess.run(prefix + ['ssh-keygen', '-A'], check=True)
    subprocess.run(prefix + ['mkdir', '-p', '/run/sshd'], check=True)
    if pathlib.Path('/run/systemd/system').exists():
        for name in ('ssh', 'sshd'):
            subprocess.run(prefix + ['systemctl', 'enable', '--now', name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if listening(port):
                return
    elif shutil.which('rc-service'):
        if shutil.which('rc-update'):
            subprocess.run(prefix + ['rc-update', 'add', 'sshd', 'default'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(prefix + ['rc-service', 'sshd', 'start'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if listening(port):
            return
    subprocess.run(prefix + [shutil.which('sshd') or '/usr/sbin/sshd', '-p', str(port)], check=True)
    if not listening(port):
        raise SystemExit('SSH 服务尚未监听指定端口 ' + str(port))


def install_frp(root, data):
    with urllib.request.urlopen(data['download_url'], timeout=120) as response:
        binary = response.read()
    if hashlib.sha256(binary).hexdigest() != data['sha256']:
        raise SystemExit('frp 下载校验失败')
    with tarfile.open(fileobj=io.BytesIO(binary), mode='r:gz') as archive:
        member = next(m for m in archive.getmembers() if m.isfile() and m.name.endswith('/frpc'))
        atomic(root / 'frpc', archive.extractfile(member).read(), 0o700)


def process_matches(pid, root, action):
    try:
        command = pathlib.Path('/proc', str(pid), 'cmdline').read_bytes().split(b'\0')
        return str(root).encode() in command and action.encode() in command and str(root / 'node.py').encode() in command
    except (OSError, ValueError):
        return False


def stop_background(root):
    for action in ('supervise', 'run'):
        try:
            pid = int((root / (action + '.pid')).read_text())
        except (OSError, ValueError):
            continue
        if process_matches(pid, root, action):
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 8
            while process_matches(pid, root, action) and time.monotonic() < deadline:
                time.sleep(.1)
            if process_matches(pid, root, action):
                os.kill(pid, signal.SIGKILL)

    # Adopt old installations which predate PID files without registering a duplicate node.
    for directory in pathlib.Path('/proc').iterdir():
        if directory.name.isdigit() and process_matches(int(directory.name), root, 'run'):
            with contextlib.suppress(ProcessLookupError):
                os.kill(int(directory.name), signal.SIGTERM)


def systemd_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def start_service(root):
    stop_background(root)
    system = os.geteuid() == 0 and pathlib.Path('/run/systemd/system').exists()
    user = not system and shutil.which('systemctl') and subprocess.run(
        ['systemctl', '--user', 'show-environment'], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL).returncode == 0
    if user:
        username = pwd.getpwuid(os.getuid()).pw_name
        linger = False
        if shutil.which('loginctl'):
            subprocess.run(['loginctl', '--no-ask-password', 'enable-linger', username],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            result = subprocess.run(['loginctl', 'show-user', username, '--property=Linger', '--value'],
                                    text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            linger = result.returncode == 0 and result.stdout.strip() == 'yes'
        if not linger:
            subprocess.run(['systemctl', '--user', 'disable', '--now', 'fleet-node'],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            user = False
    if system or user:
        ctl = ['systemctl'] + ([] if system else ['--user'])
        directory = pathlib.Path('/etc/systemd/system') if system else pathlib.Path.home() / '.config/systemd/user'
        directory.mkdir(parents=True, exist_ok=True)
        unit = ('[Unit]\nDescription=Fleet node and FRP tunnel\nAfter=network-online.target\n'
                'Wants=network-online.target\n[Service]\nExecStart=' +
                ' '.join(systemd_quote(part) for part in [sys.executable, root / 'node.py', 'run', '--root', root]) +
                '\nRestart=always\nRestartSec=2\nTimeoutStopSec=10\nUMask=0077\n[Install]\nWantedBy=' +
                ('multi-user.target' if system else 'default.target') + '\n')
        atomic(directory / 'fleet-node.service', unit, 0o644)
        subprocess.run(ctl + ['daemon-reload'], check=True)
        subprocess.run(ctl + ['enable', 'fleet-node'], check=True)
        subprocess.run(ctl + ['restart', 'fleet-node'], check=True)
        return '系统服务' if system else '用户服务'
    with (root / 'node.log').open('ab') as log:
        subprocess.Popen([sys.executable, str(root / 'node.py'), 'supervise', '--root', str(root)],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    return '后台守护进程'


def install(args):
    root = pathlib.Path(os.environ.get('FLEET_NODE_DIR', str(pathlib.Path.home() / '.local/share/fleet-node'))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    with lock(root, 'install'):
        install_locked(args, root)


def install_locked(args, root):
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    if platform.system() != 'Linux' or not arch:
        raise SystemExit('目标节点支持 Linux amd64/arm64')
    config_path, pending = root / 'config.json', root / 'enrollment-pending.json'
    cfg = json.loads(config_path.read_text()) if config_path.exists() else None
    if cfg and cfg['hub'].rstrip('/') != args.hub.rstrip('/'):
        raise SystemExit('此节点已注册到另一中枢：' + cfg['hub'])
    saved_port = (cfg or {}).get('ssh_port', 22)
    if cfg and 'ssh_port' not in cfg and (root / 'frpc.toml').exists():
        match = re.search(r'^localPort\s*=\s*(\d+)', (root / 'frpc.toml').read_text(), re.M)
        if match:
            saved_port = int(match.group(1))
    ssh_port = int(os.environ.get('FLEET_SSH_PORT', str(saved_port)))
    if not 1 <= ssh_port <= 65535:
        raise SystemExit('FLEET_SSH_PORT 必须为 1 到 65535')
    ensure_ssh(ssh_port)
    data = cfg.get('enrollment') if cfg else None
    if pending.exists():
        saved = json.loads(pending.read_text())
        if saved['hub'].rstrip('/') != args.hub.rstrip('/'):
            raise SystemExit('存在另一中枢的未完成安装：' + str(pending))
        data = saved['enrollment']
    if not cfg and not data:
        if not args.token:
            raise SystemExit('首次安装需要面板生成的注册命令')
        hostkeys = list(pathlib.Path('/etc/ssh').glob('ssh_host_ed25519_key.pub')) or list(pathlib.Path('/etc/ssh').glob('ssh_host_*_key.pub'))
        if not hostkeys:
            raise SystemExit('找不到 SSH 主机公钥')
        data = request(args.hub, '/api/enroll', args.token,
                       {'ssh_user': pwd.getpwuid(os.getuid()).pw_name, 'ssh_port': ssh_port,
                        'ssh_host_key': ' '.join(hostkeys[0].read_text().split()[:2]), 'platform': 'linux_' + arch})
        atomic(pending, json.dumps({'hub': args.hub, 'enrollment': data}))
    if data:
        if not (root / 'frpc').exists():
            install_frp(root, data)
        atomic(root / 'ca.pem', data['ca_pem'])
        atomic(root / 'frpc.toml', data['frpc_config'].replace('__CA_FILE__', str(root / 'ca.pem')))
        cfg = {'hub': args.hub.rstrip('/'), 'id': data['id'], 'token': data['token'], 'name': data['name'],
               'ssh_port': ssh_port, 'enrollment': data}
        atomic(config_path, json.dumps(cfg))
        ssh = pathlib.Path.home() / '.ssh'
        ssh.mkdir(mode=0o700, exist_ok=True)
        ssh.chmod(0o700)
        authorized = ssh / 'authorized_keys'
        if not authorized.exists() or data['public_key'] not in authorized.read_text().splitlines():
            with authorized.open('a') as output:
                output.write('\n' + data['public_key'] + '\n')
        authorized.chmod(0o600)
    for name in ('config.json', 'ca.pem', 'frpc.toml', 'frpc'):
        if not (root / name).is_file():
            raise SystemExit('旧节点缺少 ' + name + '；请恢复该文件后重跑安装命令。')
    (root / 'frpc').chmod(0o700)
    if pathlib.Path(__file__).resolve() != (root / 'node.py').resolve():
        atomic(root / 'node.py', pathlib.Path(__file__).read_bytes())
    mode = start_service(root)
    pending.unlink(missing_ok=True)
    print('已连接：' + cfg['name'] + ' (' + cfg['id'] + ')\n运行方式：' + mode + '\n面板：' + args.hub)


def cpu():
    values = list(map(int, pathlib.Path('/proc/stat').read_text().splitlines()[0].split()[1:]))
    return sum(values), values[3] + values[4]


def metrics(previous):
    current = cpu()
    delta = current[0] - previous[0]
    usage = 100 * (1 - (current[1] - previous[1]) / delta) if delta else 0
    mem = {line.split(':')[0]: int(line.split()[1]) for line in pathlib.Path('/proc/meminfo').read_text().splitlines()}
    disk = shutil.disk_usage('/')
    net = [line.split(':', 1)[1].split() for line in pathlib.Path('/proc/net/dev').read_text().splitlines()[2:] if not line.strip().startswith('lo:')]
    return current, {'cpu_percent': round(usage, 1),
                     'memory_percent': round(100 * (1 - mem.get('MemAvailable', mem['MemFree']) / mem['MemTotal']), 1),
                     'disk_percent': round(disk.used / disk.total * 100, 1),
                     'uptime': float(pathlib.Path('/proc/uptime').read_text().split()[0]),
                     'platform': platform.system() + ' ' + platform.machine(), 'load': os.getloadavg()[0],
                     'network_rx': sum(int(n[0]) for n in net), 'network_tx': sum(int(n[8]) for n in net)}


def check_update(root, cfg):
    release = request(cfg['hub'], '/api/agent-release', cfg['token'])
    expected = release['sha256']
    if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
        raise ValueError('Invalid agent release checksum')
    current = root / 'node.py'
    if hashlib.sha256(current.read_bytes()).hexdigest() == expected:
        return False
    url = urllib.parse.urljoin(cfg['hub'] + '/', release['url'])
    if urllib.parse.urlparse(url).netloc != urllib.parse.urlparse(cfg['hub']).netloc:
        raise ValueError('Agent download must use the enrolled hub')
    with urllib.request.urlopen(url, timeout=60) as response:
        source = response.read(2 * 1024 * 1024)
    if hashlib.sha256(source).hexdigest() != expected:
        raise ValueError('Agent update checksum mismatch')
    compile(source, str(current), 'exec')
    atomic(current, source)
    atomic(root / 'agent-version.json', json.dumps({'version': release.get('version', expected[:12]), 'updated_at': time.time()}))
    print('节点已更新，正在自动重连…', flush=True)
    return True


def terminate(child):
    if child and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)


def spawn_child(command):
    parent = os.getpid()
    def follow_parent():
        # Linux also terminates the child if its Python parent is killed abruptly.
        if ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
            os._exit(1)
        if os.getppid() != parent:
            os.kill(os.getpid(), signal.SIGTERM)
    return subprocess.Popen(command, preexec_fn=follow_parent)


def run(args):
    root = pathlib.Path(args.root).resolve()
    with lock(root, 'run'):
        atomic(root / 'run.pid', str(os.getpid()))
        cfg = json.loads((root / 'config.json').read_text())
        version = hashlib.sha256((root / 'node.py').read_bytes()).hexdigest()[:12]
        previous, child, next_update = cpu(), None, time.monotonic() + UPDATE_INTERVAL
        def stop(*_):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            while True:
                if child is None or child.poll() is not None:
                    child = spawn_child([str(root / 'frpc'), '-c', str(root / 'frpc.toml')])
                previous, payload = metrics(previous)
                payload['agent_version'] = version
                try:
                    request(cfg['hub'], '/api/nodes/' + cfg['id'] + '/heartbeat', cfg['token'], payload)
                except Exception as exc:
                    print('heartbeat: ' + str(exc), flush=True)
                if time.monotonic() >= next_update:
                    next_update = time.monotonic() + UPDATE_INTERVAL
                    try:
                        if check_update(root, cfg):
                            return
                    except Exception as exc:
                        print('agent update: ' + str(exc), flush=True)
                time.sleep(10)
        finally:
            terminate(child)
            (root / 'run.pid').unlink(missing_ok=True)


def supervise(args):
    root = pathlib.Path(args.root).resolve()
    with lock(root, 'supervise'):
        atomic(root / 'supervise.pid', str(os.getpid()))
        child = None
        def stop(*_):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            while True:
                child = spawn_child([sys.executable, str(root / 'node.py'), 'run', '--root', str(root)])
                child.wait()
                time.sleep(RESTART_DELAY)
        finally:
            terminate(child)
            (root / 'supervise.pid').unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='action', required=True)
    installer = sub.add_parser('install')
    installer.add_argument('--hub', required=True)
    installer.add_argument('--token', default='')
    for action in ('run', 'supervise'):
        command = sub.add_parser(action)
        command.add_argument('--root', required=True)
    args = parser.parse_args()
    {'install': install, 'run': run, 'supervise': supervise}[args.action](args)
