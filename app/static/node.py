#!/usr/bin/env python3
"""Fleet Linux node: enrollment, persistent tunnel, metrics and updates."""
import argparse
import contextlib
import collections
import csv
import fcntl
import hashlib
import http.client
import io
import json
import math
import os
import pathlib
import platform
import pwd
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import urllib.error

UPDATE_INTERVAL = 600
RESTART_DELAY = 2
LATENCY_QUEUE_LIMIT = 180
METRICS_INTERVAL = 10
METRICS_MAX_AGE = 35
FRP_READY_SECONDS = 5
BOOTSTRAP = b'''#!/usr/bin/env python3
"""Stable Fleet entry point; agent releases replace node-agent.py only."""
import os, pathlib, sys
guard = pathlib.Path(__file__).resolve().with_name('node-supervisor.py')
os.execv(sys.executable, [sys.executable, str(guard), *sys.argv[1:]])
'''


def location(cfg, environ=None):
    environ = os.environ if environ is None else environ
    saved = cfg.get('location', {})
    old_country = str(saved.get('country_code', cfg.get('country_code', ''))).strip().upper()
    new_country = str(environ.get('FLEET_COUNTRY_CODE', old_country)).strip().upper()
    moved_country = bool(re.fullmatch('[A-Z]{2}', new_country) and new_country != old_country)
    result = {}
    for key in ('country_code', 'country_name', 'region', 'city'):
        previous = '' if moved_country and key != 'country_code' else saved.get(key, cfg.get(key, ''))
        value = environ.get('FLEET_' + key.upper(), previous)
        if isinstance(value, str) and value.strip():
            value = value.strip()[:100]
            if key == 'country_code':
                value = value.upper()
                if not re.fullmatch('[A-Z]{2}', value):
                    continue
            result[key] = value
    return result


class HeartbeatLatency:
    """Acknowledge only samples included in a successful HTTPS request."""

    def __init__(self):
        self.samples = collections.deque(maxlen=LATENCY_QUEUE_LIMIT)
        self.last = None

    def send(self, cfg, payload):
        sent = list(self.samples)
        payload['latency_samples'] = sent
        if self.last is not None:
            payload.update(latency_ms=self.last['latency_ms'], latency_sample_at=self.last['timestamp'],
                           latency_success=self.last['success'])
        started = time.monotonic()
        success = False
        try:
            result = request(cfg['hub'], '/api/nodes/' + cfg['id'] + '/heartbeat', cfg['token'], payload)
            success = True
            self.samples.clear()
            return result
        finally:
            elapsed = (time.monotonic() - started) * 1000
            timestamp = round(time.time(), 3)
            self.last = {'timestamp': timestamp, 'success': success,
                         'latency_ms': round(max(0, elapsed), 3) if success else None}
            self.samples.append(self.last)


def number(value, low=0, high=None):
    if isinstance(value, dict):
        value = value.get('value')
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= low and (high is None or value <= high) else None


def gpu_row(index, vendor, name, utilization=None, used=None, total=None, temperature=None):
    return {'index': int(index), 'vendor': vendor, 'name': str(name or vendor + ' GPU')[:160],
            'util_percent': number(utilization, high=100),
            'memory_used_bytes': int(number(used)) if number(used) is not None else None,
            'memory_total_bytes': int(number(total)) if number(total) is not None else None,
            'temperature_c': number(temperature, low=-40, high=200)}


def gpu_failure(exc):
    message = str(exc).lower()
    if isinstance(exc, PermissionError) or 'permission' in message or 'insufficient' in message:
        return 'permission_denied', 'GPU 指标读取权限不足'
    if any(word in message for word in ('driver not', 'driver/library', 'couldn\'t communicate', 'not loaded',
                                        'library not found', 'driver not initialized')):
        return 'driver_missing', 'GPU 驱动或管理库不可用'
    if isinstance(exc, subprocess.TimeoutExpired):
        return 'error', 'GPU 查询超时'
    return 'error', 'GPU 指标接口不可用'


class GPUCollector:
    """A single background worker keeps GPU drivers and CLI timeouts off the heartbeat path."""

    def __init__(self):
        self.cached = {'gpus': [], 'gpu_percent': None, 'gpu_status': 'error', 'gpu_error': 'GPU 探测中'}
        self.next_probe = 0
        self.updated_at = 0
        self.worker = None
        self.nvml = None
        self.nvml_checked = False
        self.names = {}

    def snapshot(self):
        now = time.monotonic()
        if now >= self.next_probe and (self.worker is None or not self.worker.is_alive()):
            self.worker = threading.Thread(target=self._update, daemon=True)
            self.worker.start()
        result = self.cached
        if result['gpu_status'] == 'ok' and now - self.updated_at > 35:
            return {'gpus': [], 'gpu_percent': None, 'gpu_status': 'error', 'gpu_error': 'GPU 指标已过期'}
        return result

    def _update(self):
        try:
            result = self.collect()
        except Exception as exc:
            status, reason = gpu_failure(exc)
            result = {'gpus': [], 'gpu_percent': None, 'gpu_status': status, 'gpu_error': reason}
        self.cached = result
        self.updated_at = time.monotonic()
        self.next_probe = self.updated_at + (10 if result['gpu_status'] == 'ok' else 300)

    @staticmethod
    def command(arguments):
        result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, timeout=3, check=False)
        if result.returncode:
            raise RuntimeError((result.stderr or result.stdout)[:500])
        return result.stdout

    def nvidia_nvml(self):
        if not self.nvml_checked:
            self.nvml_checked = True
            try:
                import pynvml
            except ImportError:
                return None
            self.nvml = pynvml
        if self.nvml is None:
            return None
        nvml = self.nvml
        nvml.nvmlInit()
        rows = []
        try:
            for index in range(min(nvml.nvmlDeviceGetCount(), 64)):
                handle = nvml.nvmlDeviceGetHandleByIndex(index)
                key = ('nvidia', index)
                if key not in self.names:
                    name = nvml.nvmlDeviceGetName(handle)
                    self.names[key] = name.decode(errors='replace') if isinstance(name, bytes) else str(name)
                utilization = memory = temperature = None
                try:
                    utilization = nvml.nvmlDeviceGetUtilizationRates(handle).gpu
                except Exception as exc:
                    if gpu_failure(exc)[0] == 'permission_denied':
                        raise
                with contextlib.suppress(Exception):
                    memory = nvml.nvmlDeviceGetMemoryInfo(handle)
                with contextlib.suppress(Exception):
                    temperature = nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU)
                rows.append(gpu_row(index, 'nvidia', self.names[key], utilization,
                                    getattr(memory, 'used', None), getattr(memory, 'total', None), temperature))
        finally:
            with contextlib.suppress(Exception):
                nvml.nvmlShutdown()
        return rows

    def nvidia_cli(self, command):
        if not any(key[0] == 'nvidia' for key in self.names):
            names = self.command([command, '--query-gpu=index,name', '--format=csv,noheader,nounits'])
            for row in csv.reader(io.StringIO(names), skipinitialspace=True):
                if len(row) == 2 and row[0].strip().isdigit():
                    self.names[('nvidia', int(row[0]))] = row[1].strip()
        output = self.command([command, '--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu',
                               '--format=csv,noheader,nounits'])
        rows = []
        for row in csv.reader(io.StringIO(output), skipinitialspace=True):
            if len(row) != 5 or not row[0].strip().isdigit():
                continue
            index, util, used, total, temp = row
            used, total = number(used), number(total)
            rows.append(gpu_row(index, 'nvidia', self.names.get(('nvidia', int(index))), util,
                                used * 1048576 if used is not None else None,
                                total * 1048576 if total is not None else None, temp))
        if output.strip() and not rows:
            raise ValueError('Unrecognized nvidia-smi CSV')
        return rows

    def rocm_cli(self, command):
        args = [command, '--showuse', '--showmeminfo', 'vram', '--showtemp', '--json']
        names_missing = not any(key[0] == 'amd' for key in self.names)
        if names_missing:
            args.append('--showproductname')
        data = json.loads(self.command(args))
        rows = []
        for key, values in data.items():
            if not re.fullmatch('card[0-9]+', key) or not isinstance(values, dict):
                continue
            index = int(key[4:])
            name = values.get('Card Series') or values.get('Card Model')
            if name and name != 'N/A':
                self.names[('amd', index)] = name
            temp = values.get('Temperature (Sensor edge) (C)', values.get('Temperature (Sensor junction) (C)'))
            rows.append(gpu_row(index, 'amd', self.names.get(('amd', index)), values.get('GPU use (%)'),
                                values.get('VRAM Total Used Memory (B)'), values.get('VRAM Total Memory (B)'), temp))
        return rows

    @staticmethod
    def amd_rows(data):
        if isinstance(data, dict) and 'gpu' in data:
            data = [data]
        if not isinstance(data, list):
            raise ValueError('Unrecognized amd-smi JSON')
        return [row for row in data if isinstance(row, dict) and number(row.get('gpu'), high=255) is not None]

    def amd_cli(self, command):
        if not any(key[0] == 'amd' for key in self.names):
            data = self.amd_rows(json.loads(self.command([command, 'static', '--asic', '--json'])))
            for row in data:
                self.names[('amd', int(row['gpu']))] = row.get('asic', {}).get('market_name') or 'AMD GPU'
        data = self.amd_rows(json.loads(self.command([command, 'metric', '--usage', '--mem-usage', '--temperature', '--json'])))
        rows = []
        for row in data:
            index = int(row['gpu'])
            usage, memory, temp = (row.get(key) if isinstance(row.get(key), dict) else {}
                                   for key in ('usage', 'mem_usage', 'temperature'))
            util = usage.get('gfx_activity', usage.get('gfx'))
            # AMD SMI labels these quantities MB after dividing native bytes by 1024**2.
            used, total = number(memory.get('used_vram')), number(memory.get('total_vram'))
            rows.append(gpu_row(index, 'amd', self.names.get(('amd', index)), util,
                                used * 1048576 if used is not None else None,
                                total * 1048576 if total is not None else None,
                                temp.get('edge', temp.get('hotspot'))))
        return rows

    @staticmethod
    def hardware_status(pci_root='/sys/bus/pci/devices'):
        path = pathlib.Path(pci_root)
        try:
            devices = list(path.iterdir())
            gpu_vendors = []
            for device in devices:
                device_class = (device / 'class').read_text().strip().lower()
                if device_class.startswith(('0x03', '0x12')):
                    vendor = (device / 'vendor').read_text().strip().lower()
                    # Virtual VGA and BMC adapters do not provide a compute GPU.
                    if device_class.startswith('0x03') and vendor in (
                            '0x1013', '0x1234', '0x1b36', '0x1af4', '0x15ad', '0x1a03', '0x102b'):
                        continue
                    gpu_vendors.append(vendor)
        except PermissionError:
            return 'permission_denied', '无法读取 GPU 硬件信息'
        except OSError:
            return 'error', 'GPU 硬件信息不可用'
        if any(vendor in ('0x10de', '0x1002') for vendor in gpu_vendors):
            return 'driver_missing', '检测到 GPU，管理驱动或工具不可用'
        if gpu_vendors:
            return 'unsupported', '检测到显示或计算设备，暂无可用指标接口'
        return 'no_gpu', None

    def collect(self):
        devices, failures = [], []
        try:
            rows = self.nvidia_nvml()
            if rows:
                devices.extend(rows)
        except Exception as exc:
            failures.append(gpu_failure(exc))
        if not any(row['util_percent'] is not None for row in devices) and shutil.which('nvidia-smi'):
            try:
                rows = self.nvidia_cli(shutil.which('nvidia-smi'))
                if rows and (not devices or any(row['util_percent'] is not None for row in rows)):
                    devices = rows
            except Exception as exc:
                failures.append(gpu_failure(exc))
        amd_commands = [('amd-smi', self.amd_cli), ('rocm-smi', self.rocm_cli)]
        for name, collect in amd_commands:
            executable = shutil.which(name)
            if executable:
                try:
                    rows = collect(executable)
                    if rows:
                        devices.extend(rows)
                        break
                except Exception as exc:
                    failures.append(gpu_failure(exc))
        devices = devices[:64]
        if len({row['index'] for row in devices}) != len(devices):
            devices = [dict(row, index=index) for index, row in enumerate(devices)]
        valid = [row['util_percent'] for row in devices if row['util_percent'] is not None]
        if devices:
            status, reason = ('ok', None) if valid else ('error', 'GPU 已识别，核心占用指标不可用')
        elif failures:
            status, reason = next((failure for failure in failures if failure[0] == 'permission_denied'), failures[-1])
        else:
            status, reason = self.hardware_status()
        return {'gpus': devices, 'gpu_percent': round(sum(valid) / len(valid), 1) if valid else None,
                'gpu_status': status, 'gpu_error': reason}


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
    # Keep only bounded chunks in RAM, and keep the previous executable until
    # both the download checksum and extraction have succeeded.
    with tempfile.TemporaryDirectory(prefix='.frp-install-', dir=root) as folder:
        staging = pathlib.Path(folder)
        digest = hashlib.sha256()
        archive_path = staging / 'release.tar.gz'
        with urllib.request.urlopen(data['download_url'], timeout=120) as response, archive_path.open('wb') as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != data['sha256']:
            raise SystemExit('frp 下载校验失败')
        executable = staging / 'frpc'
        with tarfile.open(archive_path, mode='r|gz') as archive:
            for member in archive:
                name = pathlib.PurePosixPath(member.name)
                if name.name != 'frpc':
                    continue
                if not member.isfile() or name.is_absolute() or '..' in name.parts:
                    raise ValueError('frp 安装包中的 frpc 路径或类型无效')
                with archive.extractfile(member) as source, executable.open('wb') as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                break
            else:
                raise ValueError('frp 安装包缺少可执行文件 frpc')
        executable.chmod(0o700)
        executable.replace(root / 'frpc')


def process_matches(pid, root, action):
    try:
        command = pathlib.Path('/proc', str(pid), 'cmdline').read_bytes().split(b'\0')
        scripts = [str(root / name).encode() for name in ('node.py', 'node-agent.py', 'node-supervisor.py')]
        return str(root).encode() in command and action.encode() in command and any(script in command for script in scripts)
    except (OSError, ValueError):
        return False


def stop_background(root):
    for action in ('supervise', 'guard', 'run'):
        try:
            pid = int((root / (action + '.pid')).read_text())
        except (OSError, ValueError):
            continue
        matches = lambda: process_matches(pid, root, 'run' if action == 'guard' else action)
        if matches():
            os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 8
            while matches() and time.monotonic() < deadline:
                time.sleep(.1)
            if matches():
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
    configured_location = location(cfg or {})
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
        if configured_location:
            cfg['location'] = configured_location
        atomic(config_path, json.dumps(cfg))
        ssh = pathlib.Path.home() / '.ssh'
        ssh.mkdir(mode=0o700, exist_ok=True)
        ssh.chmod(0o700)
        authorized = ssh / 'authorized_keys'
        if not authorized.exists() or data['public_key'] not in authorized.read_text().splitlines():
            with authorized.open('a') as output:
                output.write('\n' + data['public_key'] + '\n')
        authorized.chmod(0o600)
    if configured_location and cfg.get('location') != configured_location:
        cfg['location'] = configured_location
        atomic(config_path, json.dumps(cfg))
    for name in ('config.json', 'ca.pem', 'frpc.toml', 'frpc'):
        if not (root / name).is_file():
            raise SystemExit('旧节点缺少 ' + name + '；请恢复该文件后重跑安装命令。')
    (root / 'frpc').chmod(0o700)
    source = pathlib.Path(__file__).read_bytes()
    if guarded(root):
        if hashlib.sha256(agent_path(root).read_bytes()).digest() != hashlib.sha256(source).digest():
            apply_update(root, {'source': source, 'version': hashlib.sha256(source).hexdigest()[:12]})
    else:
        release = request(cfg['hub'], '/api/agent-release', cfg['token'])
        install_guard(root, source, prepare_supervisor(cfg, release))
    mode = start_service(root)
    pending.unlink(missing_ok=True)
    print('已连接：' + cfg['name'] + ' (' + cfg['id'] + ')\n运行方式：' + mode + '\n面板：' + args.hub)


def cpu():
    values = list(map(int, pathlib.Path('/proc/stat').read_text().splitlines()[0].split()[1:]))
    return sum(values), values[3] + values[4]


class NetworkSampler:
    """Sample one route interface, or disjoint physical interfaces, without tools."""

    COUNTERS = ('network_rx', 'network_tx', 'network_rx_errors', 'network_tx_errors',
                'network_rx_dropped', 'network_tx_dropped')
    VIRTUAL_PREFIXES = ('lo', 'veth', 'docker', 'br-', 'virbr', 'vnet', 'tun', 'tap', 'wg',
                        'tailscale', 'zt', 'cni', 'flannel', 'vxlan', 'gre', 'ip6tnl', 'sit', 'ifb')

    def __init__(self, proc='/proc', interfaces='/sys/class/net'):
        self.proc, self.interfaces = pathlib.Path(proc), pathlib.Path(interfaces)
        self.previous = None
        self.cached = (0, {'network_status': 'collecting', 'network_sampled_at': None,
                           'network_rx_bps': None, 'network_tx_bps': None, 'network_interval_seconds': None,
                           'network_scope': 'unavailable', 'network_interfaces': [],
                           'network_counter_scope': 'interface_lifetime'})

    def snapshot(self):
        """Return cached observations only; safe on the heartbeat/HTTP path."""
        sampled, payload = self.cached
        result = dict(payload)
        result['network_interfaces'] = list(payload.get('network_interfaces', []))
        if (result.get('network_sampled_at') is not None and time.monotonic() - sampled > METRICS_MAX_AGE
                and result['network_status'] in ('ok', 'collecting', 'reset')):
            result.update(network_status='stale', network_rx_bps=None, network_tx_bps=None)
        return result

    def _publish(self, tick, result):
        self.cached = tick, result
        return dict(result)

    def _active(self, name):
        try:
            return (self.interfaces / name / 'operstate').read_text().strip() in ('up', 'unknown', 'dormant')
        except OSError:
            # Some containers expose /proc counters without a complete sysfs.
            return True

    def _identity(self, name):
        try:
            return int((self.interfaces / name / 'ifindex').read_text().strip())
        except (OSError, ValueError):
            return None

    def _select(self, counters):
        for version in (4, 6):
            routes = []
            try:
                lines = (self.proc / 'net' / ('route' if version == 4 else 'ipv6_route')).read_text().splitlines()
            except OSError:
                continue
            for line in lines:
                values = line.split()
                try:
                    if version == 4:
                        name, destination, mask = values[0], values[1], values[7]
                        flags, metric = int(values[3], 16), int(values[6])
                        default = destination == mask == '00000000'
                    else:
                        name = values[9]
                        flags, metric = int(values[8], 16), int(values[5], 16)
                        default = values[0] == '0' * 32 and values[1] == '00'
                    if (default and flags & 1 and not flags & 0x200 and metric >= 0
                            and name in counters and name != 'lo' and self._active(name)):
                        routes.append((metric, name))
                except (IndexError, ValueError):
                    continue
            if routes:
                # A VPN and its underlying physical NIC must not both count the
                # same bytes. Report the chosen scope rather than guessing WAN.
                return [min(routes)[1]], 'default_route'
        active = [name for name in counters if name != 'lo' and self._active(name)
                  and not (self.interfaces / name / 'master').exists()]
        physical = sorted(name for name in active if (self.interfaces / name / 'device').exists())
        if physical:
            return physical[:16], 'physical'
        fallback = sorted(name for name in active if not name.startswith(self.VIRTUAL_PREFIXES)
                          and not (self.interfaces / name / 'bridge').exists())
        # Without topology metadata, summing names can count VLAN/macvlan and
        # their parent twice. A single stable fallback has an honest scope.
        return fallback[:1], 'non_virtual' if fallback else 'unavailable'

    def sample(self):
        tick, stamp = time.monotonic(), time.time()
        result = dict(network_rx_bps=None, network_tx_bps=None, network_interval_seconds=None,
                      network_sampled_at=stamp, network_counter_scope='interface_lifetime')
        try:
            counters = {}
            for line in (self.proc / 'net/dev').read_text().splitlines()[2:]:
                if ':' not in line:
                    continue
                name, raw = line.rsplit(':', 1)
                name, fields = name.strip(), raw.split()
                try:
                    values = tuple(int(fields[index]) for index in (0, 8, 2, 10, 3, 11))
                except (ValueError, IndexError):
                    continue
                if re.fullmatch(r'[A-Za-z0-9_.:@-]{1,64}', name) and name != 'lo' and all(value >= 0 for value in values):
                    counters[name] = values
            names, scope = self._select(counters)
            result.update(network_interfaces=names, network_scope=scope)
            if not names:
                self.previous = None
                return self._publish(tick, dict(result, network_status='unavailable'))
            selected = {name: counters[name] for name in names}
            identities = {name: self._identity(name) for name in names}
            totals = tuple(sum(values[index] for values in selected.values()) for index in range(6))
            result.update(zip(self.COUNTERS, totals))
            result['network_status'] = 'collecting'
            if self.previous is not None:
                previous_tick, previous, previous_scope, previous_identities = self.previous
                elapsed = tick - previous_tick
                consistent = set(previous) == set(selected) and previous_scope == scope and previous_identities == identities
                if (consistent and elapsed > 0 and elapsed <= METRICS_MAX_AGE
                        and all(all(now >= old for now, old in zip(selected[name], previous[name])) for name in names)):
                    result.update(network_rx_bps=round(sum(selected[name][0] - previous[name][0] for name in names) / elapsed, 3),
                                  network_tx_bps=round(sum(selected[name][1] - previous[name][1] for name in names) / elapsed, 3),
                                  network_interval_seconds=round(elapsed, 3), network_status='ok')
                else:
                    result['network_status'] = 'reset'
            self.previous = tick, selected, scope, identities
            return self._publish(tick, result)
        except (OSError, ValueError):
            self.previous = None
            return self._publish(tick, dict(result, network_status='error', network_interfaces=[], network_scope='unavailable'))


def metrics(previous):
    current = cpu()
    delta = current[0] - previous[0] if previous is not None else 0
    usage = 100 * (1 - (current[1] - previous[1]) / delta) if delta > 0 else None
    mem = {line.split(':')[0]: int(line.split()[1]) for line in pathlib.Path('/proc/meminfo').read_text().splitlines()}
    memory_total = mem['MemTotal'] * 1024
    memory_used = max(0, memory_total - mem.get('MemAvailable', mem['MemFree']) * 1024)
    disk = shutil.disk_usage('/')
    return current, {'cpu_percent': round(usage, 1) if usage is not None else None,
                     'memory_percent': round(100 * (1 - mem.get('MemAvailable', mem['MemFree']) / mem['MemTotal']), 1),
                     'memory_used_bytes': memory_used, 'memory_total_bytes': memory_total,
                     'disk_percent': round(disk.used / disk.total * 100, 1),
                     'disk_used_bytes': disk.used, 'disk_total_bytes': disk.total,
                     'uptime': float(pathlib.Path('/proc/uptime').read_text().split()[0]),
                     'platform': platform.system() + ' ' + platform.machine(), 'load': os.getloadavg()[0]}


class MetricsCollector:
    """One persistent worker keeps slow filesystem reads off the control path."""

    def __init__(self):
        self.previous = None
        self.network = NetworkSampler()
        self.cached = (0, {'metrics_status': 'collecting', 'metrics_sampled_at': None, 'metrics_error': None})
        self.worker = None
        self.stopped = threading.Event()

    def snapshot(self):
        if self.worker is None and not self.stopped.is_set():
            self.worker = threading.Thread(target=self._run, daemon=True)
            self.worker.start()
        sampled, result = self.cached
        network = self.network.snapshot()
        if result['metrics_status'] == 'ok' and time.monotonic() - sampled > METRICS_MAX_AGE:
            return {'metrics_status': 'stale', 'metrics_sampled_at': result['metrics_sampled_at'],
                    'metrics_error': '基础指标已过期', **network}
        # The heartbeat adds agent/GPU fields; never mutate the worker's cached sample.
        return {**{key: value for key, value in result.items() if not key.startswith('network_')}, **network}

    def _update(self):
        # CPU/memory are read before disk usage, which may block on a slow filesystem.
        # Age the entire sample from collection start so a late result cannot look fresh.
        started, sampled_at = time.monotonic(), time.time()
        network = self.network.sample()
        try:
            previous, payload = metrics(self.previous)
            self.previous = previous
            result = dict(payload, metrics_status='ok', metrics_sampled_at=sampled_at, metrics_error=None)
        except Exception:
            result = {'metrics_status': 'error', 'metrics_sampled_at': self.cached[1]['metrics_sampled_at'],
                      'metrics_error': '基础指标采集失败'}
        self.cached = (started, dict(result, **network))

    def _run(self):
        while not self.stopped.is_set():
            self._update()
            self.stopped.wait(METRICS_INTERVAL)

    def stop(self):
        # A blocked statvfs/read cannot be cancelled safely; the daemon ends with the process.
        self.stopped.set()


def agent_path(root):
    # A partially staged migration may already contain node-agent.py while the
    # service still starts the old complete node.py. Hash that running layout.
    return root / ('node-agent.py' if guarded(root) else 'node.py')


def guarded(root):
    return ((root / 'node-supervisor.py').is_file() and (root / 'node.py').is_file()
            and (root / 'node.py').read_bytes() == BOOTSTRAP)


def update_state(root):
    try:
        value = json.loads((root / 'agent-update.json').read_text())
        if not isinstance(value, dict):
            raise ValueError('Invalid agent update state')
        return value
    except FileNotFoundError:
        return {}


def release_source(cfg, metadata, name):
    expected = metadata['sha256']
    if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
        raise ValueError('Invalid ' + name + ' release checksum')
    url = urllib.parse.urljoin(cfg['hub'] + '/', metadata['url'])
    parsed, enrolled = urllib.parse.urlparse(url), urllib.parse.urlparse(cfg['hub'])
    if (parsed.scheme, parsed.netloc) != (enrolled.scheme, enrolled.netloc):
        raise ValueError('Agent download must use the enrolled hub')
    with urllib.request.urlopen(url, timeout=60) as response:
        source = response.read(2 * 1024 * 1024)
    if hashlib.sha256(source).hexdigest() != expected:
        raise ValueError('Agent update checksum mismatch')
    compile(source, name, 'exec')
    return source


def prepare_supervisor(cfg, release):
    if not isinstance(release.get('supervisor'), dict):
        raise ValueError('Hub does not publish the stable agent supervisor')
    return release_source(cfg, release['supervisor'], 'node-supervisor.py')


def install_guard(root, source, supervisor):
    # Write the stable entry point last. Until then old services still start the
    # complete agent; afterward both old systemd and background commands use the guard.
    atomic(root / 'node-supervisor.py', supervisor)
    atomic(root / 'node-agent.py', source)
    atomic(root / 'node.py', BOOTSTRAP)


def prepare_update(root, cfg):
    """Fetch and validate a release without changing the running agent on disk."""
    release = request(cfg['hub'], '/api/agent-release', cfg['token'])
    expected = release['sha256']
    if not isinstance(expected, str) or not re.fullmatch('[0-9a-f]{64}', expected):
        raise ValueError('Invalid agent release checksum')
    state = update_state(root)
    if state.get('pending') or expected in state.get('rejected', []):
        return None
    current = agent_path(root).read_bytes()
    same = hashlib.sha256(current).hexdigest() == expected
    protected = guarded(root)
    if same and protected:
        return None
    result = {'source': current if same else release_source(cfg, release, 'node-agent.py'),
              'version': release.get('version', expected[:12])}
    if not protected:
        result['supervisor_source'] = prepare_supervisor(cfg, release)
    return result


def apply_update(root, release):
    # Only the main loop commits an update, after the worker has finished.
    if not guarded(root):
        # The first migration cannot protect an older client before it has
        # successfully loaded this implementation and fetched the guard.
        current = agent_path(root).read_bytes()
        install_guard(root, current, release['supervisor_source'])
        if current != release['source']:
            return apply_update(root, release)
    else:
        current = update_state(root)
        if current.get('pending'):
            raise ValueError('An agent update is still being verified')
        previous = agent_path(root).read_bytes()
        expected = hashlib.sha256(release['source']).hexdigest()
        atomic(root / 'node-agent.previous.py', previous)
        current['pending'] = {'sha256': expected, 'previous_sha256': hashlib.sha256(previous).hexdigest(),
                              'version': release['version']}
        current['updated_at'] = time.time()
        atomic(root / 'agent-update.json', json.dumps(current))
        try:
            atomic(root / 'node-agent.py', release['source'])
        except OSError:
            current['pending'] = None
            atomic(root / 'agent-update.json', json.dumps(current))
            raise
    try:
        atomic(root / 'agent-version.json', json.dumps({'version': release['version'], 'updated_at': time.time()}))
    except OSError as exc:
        # The new source is already installed: restart even if this optional marker cannot be saved.
        print('agent version marker: ' + str(exc), flush=True)
    print('节点已更新，正在自动重连…', flush=True)
    return True


def check_update(root, cfg):
    release = prepare_update(root, cfg)
    return apply_update(root, release) if release is not None else False


class AgentUpdater:
    """Keep release HTTP requests off the heartbeat and FRP supervision path."""

    def __init__(self, root, cfg):
        self.root, self.cfg = root, cfg
        self.next_check = time.monotonic() + UPDATE_INTERVAL
        self.worker = None
        self.pending = None
        self.error = None
        self.checked_at = None
        self.check_failed = False
        self.deferred = False
        if not guarded(root):
            self.next_check = 0

    def _prepare(self):
        try:
            self.pending = prepare_update(self.root, self.cfg)
            self.check_failed = False
        except Exception as exc:
            self.error = exc
            self.check_failed = True
        finally:
            self.checked_at = time.time()

    def status(self):
        try:
            saved = update_state(self.root)
        except (OSError, ValueError):
            return {'status': 'check_failed', 'error': 'local_state', 'checked_at': self.checked_at}
        pending = saved.get('pending')
        return {'status': 'pending' if pending else ('deferred' if self.deferred else ('check_failed' if self.check_failed else saved.get('last_result', 'idle'))),
                'version': pending.get('version') if pending else (self.pending.get('version') if self.pending else saved.get('last_version')),
                'updated_at': saved.get('updated_at'), 'checked_at': self.checked_at,
                'error': 'release_check' if self.check_failed else saved.get('reason')}

    def poll(self, allow_apply=True):
        if self.worker is not None:
            if self.worker.is_alive():
                return False
            self.worker = None
            self.next_check = time.monotonic() + UPDATE_INTERVAL
            error = self.error
            self.error = None
            if error is not None:
                print('agent update: ' + str(error), flush=True)
                self.pending = None
        if self.pending is not None:
            self.deferred = not allow_apply
            if not allow_apply:
                return False
            release, self.pending = self.pending, None
            try:
                return apply_update(self.root, release)
            except Exception as exc:
                print('agent update: ' + str(exc), flush=True)
                self.check_failed = True
            return False
        if time.monotonic() >= self.next_check:
            self.worker = threading.Thread(target=self._prepare, daemon=True)
            self.worker.start()
        return False


class RelayFailure(Exception):
    """Only fixed, credential-free failure codes may reach relay progress."""

    def __init__(self, code, retryable=False):
        super().__init__(code)
        self.code, self.retryable = code, retryable


class RelayNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib copies Authorization when following redirects. Resolve the hub
        # redirect explicitly, then create a fresh request with no node token.
        return None


class RelayReceiver:
    """One bounded-memory worker receives files without delaying heartbeats."""

    BUFFER = 1024 * 1024
    MANIFEST_LIMIT = 4 * 1024 * 1024
    RETRIES = 3

    def __init__(self, root, cfg, open_url=None):
        self.root, self.cfg = pathlib.Path(root), cfg
        self.open_url = open_url or urllib.request.build_opener(RelayNoRedirect()).open
        self.worker = None
        self.stopped = threading.Event()
        self.last_error = None
        self.progress_error = None
        self.next_retry = 0

    @property
    def busy(self):
        return self.worker is not None and self.worker.is_alive()

    def poll(self, available=False):
        if (available is True and not self.busy and not self.stopped.is_set()
                and time.monotonic() >= self.next_retry):
            self.worker = threading.Thread(target=self._receive, name='FleetRelayReceiver', daemon=True)
            self.worker.start()

    def stop(self):
        self.stopped.set()

    def _check_stopped(self):
        if self.stopped.is_set():
            raise RelayFailure('receiver_stopped', True)

    def _retry_after(self, response):
        if response.code not in (429, 503):
            return
        try:
            delay = float(response.headers.get('Retry-After', '10'))
            if not math.isfinite(delay) or delay < 0:
                delay = 10
        except ValueError:
            delay = 10
        self.next_retry = time.monotonic() + max(1, delay)

    def _hub(self, path, payload=None, redirect=False, timeout=20):
        self._check_stopped()
        headers = {'Authorization': 'Bearer ' + self.cfg['token'], 'Content-Type': 'application/json'}
        body = None if payload is None else json.dumps(payload).encode()
        req = urllib.request.Request(self.cfg['hub'].rstrip('/') + path, data=body, headers=headers)
        try:
            with self.open_url(req, timeout=timeout) as response:
                if redirect:
                    raise RelayFailure('invalid_download_redirect')
                data = response.read(self.MANIFEST_LIMIT + 1)
                if len(data) > self.MANIFEST_LIMIT:
                    raise RelayFailure('invalid_manifest')
                return json.loads(data)
        except urllib.error.HTTPError as exc:
            try:
                if redirect and exc.code in (302, 303, 307, 308):
                    return self._download_url(exc.headers.get('Location', ''))
                if exc.code in (401, 403, 404, 410):
                    raise RelayFailure('task_unavailable') from None
                if exc.code == 409:
                    raise RelayFailure('source_changed') from None
                self._retry_after(exc)
                raise RelayFailure('relay_control_failed', exc.code == 429 or exc.code >= 500) from None
            finally:
                exc.close()
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            raise RelayFailure('relay_control_unavailable', True) from None
        except (ValueError, UnicodeError):
            raise RelayFailure('invalid_manifest') from None

    @staticmethod
    def _download_url(url):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.fragment):
            raise RelayFailure('invalid_download_redirect')
        return url

    def _progress(self, task, file_id=None, done=0, status='downloading', error=None, timeout=20):
        payload = {'file_id': file_id, 'bytes_done': done, 'status': status}
        if error is not None:
            payload['error'] = error
        return self._hub('/api/nodes/' + self.cfg['id'] + '/relay-tasks/' + task['id'] + '/progress', payload,
                         timeout=timeout)

    @staticmethod
    def _relative(value):
        if not isinstance(value, str) or not value or len(value) > 4096 or '\\' in value or '\0' in value:
            raise RelayFailure('unsafe_destination')
        path = pathlib.PurePosixPath(value)
        if path.is_absolute() or any(part in ('', '.', '..') for part in value.split('/')):
            raise RelayFailure('unsafe_destination')
        return path

    @staticmethod
    def _identifier(value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
            raise RelayFailure('invalid_manifest')
        return value

    @staticmethod
    @contextlib.contextmanager
    def _directory(path, create=True):
        """Walk with dirfds so symlink replacement cannot redirect our writes."""
        if not path.is_absolute() or '..' in path.parts:
            raise RelayFailure('unsafe_destination')
        fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in path.parts[1:]:
                try:
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    try:
                        os.mkdir(component, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _stat(fd, name):
        try:
            result = os.stat(name, dir_fd=fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(result.st_mode):
            raise RelayFailure('unsafe_destination')
        return result

    @staticmethod
    def _signature(info):
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]

    @staticmethod
    def _save(fd, name, data):
        temporary = name + '.' + os.urandom(8).hex() + '.tmp'
        file_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(file_fd, 'wb') as handle:
                handle.write(json.dumps(data, separators=(',', ':')).encode())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=fd)

    def _manifest(self, task):
        if not isinstance(task, dict):
            raise RelayFailure('invalid_manifest')
        self._identifier(task.get('id'))
        destination = task.get('destination')
        if (not isinstance(destination, str) or not destination or len(destination) > 4096
                or '\0' in destination or '\\' in destination):
            raise RelayFailure('unsafe_destination')
        base = pathlib.Path(destination).expanduser()
        if not base.is_absolute():
            base = pathlib.Path.home() / base
        if '..' in base.parts:
            raise RelayFailure('unsafe_destination')
        files, directories = task.get('files'), task.get('directories', [])
        if (type(task.get('is_directory')) is not bool or type(task.get('overwrite')) is not bool
                or not isinstance(files, list) or not isinstance(directories, list)
                or len(files) + len(directories) > 10000
                or (not task['is_directory'] and (len(files) != 1 or directories))):
            raise RelayFailure('invalid_manifest')
        # collect_files includes '' for the source root; base is created below.
        relative_dirs = [self._relative(value) for value in directories if value != '']
        names, identifiers, targets = set(), set(), []
        for item in files:
            if not isinstance(item, dict):
                raise RelayFailure('invalid_manifest')
            identifier = self._identifier(item.get('id'))
            relative = self._relative(item.get('path'))
            if (identifier in identifiers or str(relative) in names or type(item.get('size')) is not int
                    or not 0 <= item['size'] <= 10 * 1024 ** 3 or not isinstance(item.get('etag'), str)
                    or not item['etag'] or len(item['etag']) > 1024):
                raise RelayFailure('invalid_manifest')
            expected = '/api/relay/transfers/' + task['id'] + '/files/' + identifier + '/content'
            if item.get('download_path') != expected:
                raise RelayFailure('invalid_manifest')
            identifiers.add(identifier)
            names.add(str(relative))
            target = base / relative if task['is_directory'] else base
            if not task['is_directory'] and (destination.endswith('/') or base.is_dir()):
                target = base / relative.name
            targets.append((item, target))
        if sum(item['size'] for item in files) > 10 * 1024 ** 3:
            raise RelayFailure('invalid_manifest')
        for name in names:
            path = pathlib.PurePosixPath(name)
            if any(str(parent) in names for parent in path.parents):
                raise RelayFailure('unsafe_destination')
        if any(str(value) in names or any(str(parent) in names for parent in value.parents)
               for value in relative_dirs):
            raise RelayFailure('unsafe_destination')
        return base, relative_dirs, targets

    def _open_download(self, url, offset):
        for _ in range(5):
            headers = {'Range': 'bytes=' + str(offset) + '-'} if offset else {}
            # Fresh request, deliberately no Authorization or hub cookies.
            req = urllib.request.Request(self._download_url(url), headers=headers)
            try:
                return self.open_url(req, timeout=30)
            except urllib.error.HTTPError as exc:
                try:
                    if exc.code in (301, 302, 303, 307, 308):
                        url = urllib.parse.urljoin(url, exc.headers.get('Location', ''))
                        continue
                    if exc.code in (403, 404):
                        raise RelayFailure('download_url_expired', True) from None
                    if exc.code == 412:
                        raise RelayFailure('source_changed') from None
                    self._retry_after(exc)
                    raise RelayFailure('download_failed', exc.code == 429 or exc.code >= 500) from None
                finally:
                    exc.close()
            except (urllib.error.URLError, OSError, http.client.HTTPException):
                raise RelayFailure('download_unavailable', True) from None
        raise RelayFailure('invalid_download_redirect')

    def _stream(self, task, item, output):
        self._check_stopped()
        offset = output.seek(0, os.SEEK_END)
        if offset > item['size']:
            raise RelayFailure('partial_size_mismatch')
        # An empty file still needs server-side validation of its item version.
        url = self._hub(item['download_path'], redirect=True)
        if offset == item['size'] and item['size']:
            return False
        with self._open_download(url, offset) as response:
            status = response.status
            if status == 206:
                match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                if (not match or int(match[1]) != offset or int(match[3]) != item['size']
                        or int(match[2]) != item['size'] - 1):
                    raise RelayFailure('invalid_download_range')
            elif status == 200:
                # Some download hosts ignore Range. Never append a full body.
                output.seek(0)
                output.truncate()
                offset = 0
            else:
                raise RelayFailure('invalid_download_response')
            length = response.headers.get('Content-Length')
            if length is not None and (not length.isdigit() or int(length) != item['size'] - offset):
                raise RelayFailure('source_size_mismatch')
            checked = time.monotonic()
            while True:
                self._check_stopped()
                try:
                    chunk = getattr(response, 'read1', response.read)(self.BUFFER)
                except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError):
                    raise RelayFailure('download_interrupted', True) from None
                if not chunk:
                    break
                if offset + len(chunk) > item['size']:
                    raise RelayFailure('source_size_mismatch')
                if output.write(chunk) != len(chunk):
                    raise RelayFailure('destination_write_failed')
                offset += len(chunk)
                now = time.monotonic()
                if now - checked >= 5 and now >= self.next_retry:
                    try:
                        # Progress is advisory: a slow control plane must not
                        # make every chunk wait or discard a healthy CDN stream.
                        self._progress(task, item['id'], offset, timeout=4)
                        self.progress_error = None
                    except RelayFailure as exc:
                        if not exc.retryable:
                            raise
                        self.progress_error = exc.code
                        self.next_retry = max(self.next_retry, time.monotonic() + 30)
                    checked = time.monotonic()
            if offset != item['size']:
                raise RelayFailure('download_interrupted', True)
        return True

    def _file(self, task, item, target):
        binding = {'task': task['id'], 'file': item['id'], 'size': item['size'],
                   'etag': item['etag'], 'destination': str(target)}
        stem = '.fleet-relay-' + hashlib.sha256((task['id'] + ':' + item['id']).encode()).hexdigest()[:32]
        partial, marker = stem + '.part', stem + '.json'
        with self._directory(target.parent) as fd:
            target_info = self._stat(fd, target.name)
            saved = None
            if self._stat(fd, marker) is not None:
                marker_fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
                with os.fdopen(marker_fd, 'rb') as handle:
                    raw = handle.read(16385)
                if len(raw) > 16384:
                    raise RelayFailure('invalid_resume_state')
                try:
                    saved = json.loads(raw)
                except (ValueError, UnicodeError):
                    raise RelayFailure('invalid_resume_state') from None
                if not isinstance(saved, dict) or saved.get('binding') != binding:
                    raise RelayFailure('source_changed')
                if saved.get('phase') not in ('downloading', 'ready', 'committed'):
                    raise RelayFailure('invalid_resume_state')
                if saved.get('phase') in ('ready', 'committed') and target_info is not None:
                    if saved.get('signature') == self._signature(target_info):
                        leftover = self._stat(fd, partial)
                        if leftover is not None and self._signature(leftover) == saved['signature']:
                            os.unlink(partial, dir_fd=fd)
                        saved['phase'] = 'committed'
                        self._save(fd, marker, saved)
                        self._progress(task, item['id'], item['size'], 'completed')
                        return
                if saved.get('phase') == 'committed':
                    raise RelayFailure('destination_changed')
            if target_info is not None and not task['overwrite']:
                raise RelayFailure('destination_exists')
            if saved is None:
                if self._stat(fd, partial) is not None:
                    # No durable binding means these bytes cannot be trusted.
                    raise RelayFailure('invalid_resume_state')
                saved = {'binding': binding, 'phase': 'downloading'}
                self._save(fd, marker, saved)
            file_fd = os.open(partial, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            with os.fdopen(file_fd, 'r+b', buffering=0) as output:
                partial_info = os.fstat(output.fileno())
                if not stat.S_ISREG(partial_info.st_mode) or partial_info.st_nlink != 1:
                    raise RelayFailure('unsafe_destination')
                for attempt in range(self.RETRIES):
                    try:
                        downloaded = self._stream(task, item, output)
                        if downloaded:
                            # The signed URL's version binding is not assumed.
                            # Recheck only Graph metadata and task authorization,
                            # never a second file body or full-file hash.
                            self._hub(item['download_path'], redirect=True)
                        break
                    except RelayFailure as exc:
                        if (not exc.retryable or attempt == self.RETRIES - 1 or self.stopped.is_set()
                                or time.monotonic() < self.next_retry):
                            raise
                output.flush()
                os.fsync(output.fileno())
                finished = os.fstat(output.fileno())
                if finished.st_size != item['size']:
                    raise RelayFailure('destination_write_failed')
                saved.update(phase='ready', signature=self._signature(finished))
                self._save(fd, marker, saved)
            # Verify the target again immediately before committing. dirfd-based
            # rename/link does not follow a symlink even if another process races.
            self._stat(fd, target.name)
            if task['overwrite']:
                os.replace(partial, target.name, src_dir_fd=fd, dst_dir_fd=fd)
            else:
                try:
                    os.link(partial, target.name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                except FileExistsError:
                    raise RelayFailure('destination_exists') from None
                os.unlink(partial, dir_fd=fd)
            os.fsync(fd)
            saved['phase'] = 'committed'
            self._save(fd, marker, saved)
            # Ack only after both data and the atomic rename are durable.
            self._progress(task, item['id'], item['size'], 'completed')

    def _receive(self):
        task = None
        current = None
        try:
            envelope = self._hub('/api/nodes/' + self.cfg['id'] + '/relay-task')
            if not isinstance(envelope, dict):
                raise RelayFailure('invalid_manifest')
            task = envelope.get('task')
            if task is None:
                return
            base, directories, files = self._manifest(task)
            self._progress(task)
            if task['is_directory']:
                with self._directory(base):
                    pass
                for relative in directories:
                    with self._directory(base / relative):
                        pass
            for item, target in files:
                current = item['id']
                if item.get('status') != 'completed':
                    self._file(task, item, target)
            self._progress(task, status='completed')
            self.last_error = None
        except Exception as exc:
            failure = exc if isinstance(exc, RelayFailure) else RelayFailure('destination_unavailable')
            self.last_error = failure.code
            if (not failure.retryable and failure.code != 'task_unavailable' and isinstance(task, dict)
                    and isinstance(task.get('id'), str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', task['id'])):
                with contextlib.suppress(Exception):
                    self._progress(task, current, status='failed', error=failure.code)


def terminate(child):
    if child and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)


def spawn_child(command):
    # Never run Python in preexec_fn: FRP can restart while metrics/update
    # threads hold locks. This short-lived interpreter sets the Linux parent
    # death signal after exec, checks the startup race, then replaces itself.
    bootstrap = '''import ctypes, os, signal, sys
if ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
    sys.exit(1)
if os.getppid() != int(sys.argv[1]):
    sys.exit(1)
for name in ('SIGPIPE', 'SIGXFZ', 'SIGXFSZ'):
    if hasattr(signal, name):
        signal.signal(getattr(signal, name), signal.SIG_DFL)
os.execvp(sys.argv[2], sys.argv[2:])
'''
    return subprocess.Popen([sys.executable, '-I', '-S', '-c', bootstrap, str(os.getpid()), *command])


class CandidateReadiness:
    """Report local startup and FRP survival only during candidate probation.

    A slow HTTPS request must not turn a healthy local release into a rejected
    version. This temporary reporter intentionally does not certify network
    reachability or main-loop responsiveness.
    """

    def __init__(self, fd):
        self.fd = fd
        os.set_inheritable(fd, False)
        os.set_blocking(fd, False)
        self.stopped = threading.Event()
        self.mutex = threading.Lock()
        self.target = (None, None)
        self.worker = None

    def watch(self, child, started):
        with self.mutex:
            self.target = (child, started)
            if self.worker is None and not self.stopped.is_set():
                self.worker = threading.Thread(target=self._report, name='FleetCandidateReadiness', daemon=True)
                self.worker.start()

    def _close(self):
        # All writes and closes share a lock: a concurrent stop cannot close a
        # descriptor and let a late reporter write into its newly reused number.
        with self.mutex:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None

    def _report(self):
        try:
            while not self.stopped.is_set():
                with self.mutex:
                    target = self.target
                child, started = target
                ready = child is not None and child.poll() is None and time.monotonic() - started >= FRP_READY_SECONDS
                with self.mutex:
                    if self.fd is None or self.stopped.is_set():
                        return
                    try:
                        os.write(self.fd, b'.' if ready and self.target is target else b'!')
                    except BlockingIOError:
                        # The guard drains this tiny pipe; a slow reader must not
                        # prevent shutdown or block the main loop.
                        pass
                self.stopped.wait(min(1, FRP_READY_SECONDS / 2))
        except (BrokenPipeError, OSError):
            # Confirmation closes the guard's pipe. End the probation-only
            # worker instead of leaving another steady-state thread behind.
            pass
        finally:
            self.stopped.set()
            self._close()

    def stop(self):
        self.stopped.set()
        worker = self.worker
        if worker is not None and worker.ident is not None:
            worker.join(timeout=2)
        self._close()


def run(args):
    root = pathlib.Path(args.root).resolve()
    with lock(root, 'run'):
        atomic(root / 'run.pid', str(os.getpid()))
        cfg = json.loads((root / 'config.json').read_text())
        version = hashlib.sha256(agent_path(root).read_bytes()).hexdigest()[:12]
        child = None
        child_started = None
        resources, gpu = MetricsCollector(), GPUCollector()
        latency, updater = HeartbeatLatency(), AgentUpdater(root, cfg)
        relay = RelayReceiver(root, cfg)
        ready_fd = os.environ.get('FLEET_AGENT_READY_FD')
        readiness = CandidateReadiness(int(ready_fd)) if ready_fd else None
        def stop(*_):
            raise SystemExit(0)
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            while True:
                if child is None or child.poll() is not None:
                    child = spawn_child([str(root / 'frpc'), '-c', str(root / 'frpc.toml')])
                    child_started = time.monotonic()
                payload = resources.snapshot()
                payload['agent_version'] = version
                payload['relay_protocol'] = 1
                payload['agent_update'] = updater.status()
                payload.update(location(cfg))
                payload.update(gpu.snapshot())
                # Complete local initialization before allowing readiness. From
                # here onward network latency is independent of candidate health.
                if readiness is not None:
                    readiness.watch(child, child_started)
                update_allowed = False
                try:
                    response = latency.send(cfg, payload)
                    update_allowed = isinstance(response, dict) and response.get('update_allowed', True) is True
                    relay.poll(available=isinstance(response, dict) and response.get('relay_available') is True)
                except Exception as exc:
                    print('heartbeat: ' + str(exc), flush=True)
                if updater.poll(allow_apply=update_allowed and not relay.busy):
                    return
                time.sleep(10)
        finally:
            relay.stop()
            resources.stop()
            if readiness is not None:
                readiness.stop()
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
