#!/usr/bin/env python3
"""Publish Fleet app/wheels from Git, with an atomic release switch and rollback."""
from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.request


class UpdateInterrupted(RuntimeError):
    pass


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.chmod(0o644)
    os.replace(temp, path)


def switch(link, target):
    link, target = Path(link), Path(target)
    temp = link.with_name(link.name + '.next')
    temp.unlink(missing_ok=True)
    temp.symlink_to(target, target_is_directory=True)
    os.replace(temp, link)


class Updater:
    def __init__(self, config):
        self.cfg = config
        self.base = Path(config.get('base_dir', '/opt/fleet'))
        self.data = Path(config.get('data_dir', '/var/lib/fleet'))
        self.repo = Path(config.get('repo_path', str(self.base / 'repo')))
        self.current = self.base / 'current'
        self.releases = self.base / 'releases'
        self.python = config.get('python', '/opt/fleet/venv/bin/python')
        self.status_path = self.data / 'update-status.json'
        try:
            self.status = json.loads(self.status_path.read_text())
        except (OSError, ValueError):
            self.status = {}
        self.env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
        if config.get('deploy_key'):
            self.env['GIT_SSH_COMMAND'] = shlex.join([
                'ssh', '-i', config['deploy_key'], '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15',
                '-o', 'UserKnownHostsFile=' + config['known_hosts'],
            ])

    def write_status(self, status, message, **fields):
        summary = message if len(message) <= 1000 else message[:200] + '\n...\n' + message[-795:]
        self.status.update(status=status, message=summary, **fields)
        atomic_json(self.status_path, self.status)
        print(status + ': ' + message, flush=True)

    def command(self, args, **kwargs):
        result = subprocess.run(args, env=kwargs.pop('env', self.env), text=True,
                                capture_output=True, timeout=kwargs.pop('timeout', 120), **kwargs)
        if result.returncode:
            raise RuntimeError(f'{Path(args[0]).name} failed: {(result.stderr or result.stdout)[-1600:].strip()}')
        return result.stdout.strip()

    def fetch(self):
        self.command(['git', '-c', 'http.version=HTTP/1.1', '-C', str(self.repo), 'fetch', '--no-tags', '--depth=1',
                      'origin', self.cfg.get('branch', 'main')])
        commit = self.command(['git', '-C', str(self.repo), 'rev-parse', 'FETCH_HEAD'])
        if not re.fullmatch(r'[a-f0-9]{40,64}', commit):
            raise RuntimeError('Git returned an invalid commit ID')
        return commit

    def reusable_vendor(self, dependency_key):
        """Reuse only a prepared active release, with independent files for rollback."""
        try:
            previous = self.current.resolve(strict=True)
            metadata = json.loads((previous / 'release.json').read_text())
            vendor = previous / 'vendor'
            if (metadata.get('dependency_key') == dependency_key and vendor.is_dir()
                    and not vendor.is_symlink()
                    and not any(path.is_symlink() for path in vendor.rglob('*'))):
                return vendor
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return None

    def prepare(self, commit):
        started = time.monotonic()
        release = self.releases / commit
        self.releases.mkdir(parents=True, exist_ok=True)
        if release.exists():
            shutil.rmtree(release)
        staging = Path(tempfile.mkdtemp(prefix='.prepare-', dir=self.releases))
        try:
            wheels_tree = self.command(['git', '-C', str(self.repo), 'rev-parse', commit + ':wheels'])
            python_runtime = json.loads(self.command([self.python, '-c',
                'import json, platform, sys; print(json.dumps([sys.version, sys.executable, '
                'sys.prefix, platform.machine()]))']))
            dependency_key = {'wheels_tree': wheels_tree, 'python': python_runtime}
            cached_vendor = self.reusable_vendor(dependency_key)
            archive = staging / 'source.tar'
            with archive.open('wb') as output:
                subprocess.run(['git', '-C', str(self.repo), 'archive', commit, 'app',
                                *([] if cached_vendor else ['wheels'])],
                               env=self.env, stdout=output, stderr=subprocess.PIPE, check=True, timeout=60)
            with tarfile.open(archive) as package:
                package.extractall(staging, filter='data')
            archive.unlink()
            for required in ('app.py', 'mcp_entry.py', 'static/index.html'):
                if not (staging / 'app' / required).is_file():
                    raise RuntimeError('Release is missing app/' + required)
            downloads = staging / 'app' / 'downloads'
            if downloads.is_symlink():
                raise RuntimeError('Git downloads directory must contain only .gitkeep')
            if downloads.exists():
                if not downloads.is_dir() or any(p.name != '.gitkeep' or not p.is_file() or p.is_symlink()
                                                 for p in downloads.iterdir()):
                    raise RuntimeError('Git downloads directory must contain only .gitkeep')
                shutil.rmtree(downloads)
            downloads.symlink_to(self.base / 'shared' / 'downloads', target_is_directory=True)
            vendor = staging / 'vendor'
            if cached_vendor:
                # Do not hard-link or share a mutable vendor across rollback releases.
                shutil.copytree(cached_vendor, vendor)
            else:
                vendor.mkdir()
                wheels = sorted((staging / 'wheels').glob('*.whl'))
                if wheels:
                    self.command([self.python, '-m', 'pip', 'install', '--no-index', '--no-deps',
                                  '--no-compile', '--target', str(vendor), *map(str, wheels)])
            self.command([self.python, '-m', 'compileall', '-q', str(staging / 'app')])
            env = dict(self.env, PYTHONPATH=str(vendor))
            dependency_check = '''
from importlib import metadata
from pip._vendor.packaging.requirements import Requirement
import os
for distribution in metadata.distributions(path=[os.environ['PYTHONPATH']]):
    for spec in distribution.requires or []:
        requirement = Requirement(spec)
        if requirement.marker and not requirement.marker.evaluate({'extra': ''}):
            continue
        version = metadata.version(requirement.name)
        if version not in requirement.specifier:
            raise RuntimeError(f'{requirement.name} {version} does not satisfy {spec}')
import fastapi, uvicorn, httpx, asyncssh, relay_shell.server
'''
            self.command([self.python, '-c', dependency_check], env=env)
            # Wheels remain recoverable from Git and are not needed by the running release.
            if (staging / 'wheels').exists():
                shutil.rmtree(staging / 'wheels')
            atomic_json(staging / 'release.json', {
                'commit': commit, 'prepared_at': now(), 'dependency_key': dependency_key,
                'dependencies_reused': cached_vendor is not None,
                'prepare_seconds': round(time.monotonic() - started, 3),
            })
            # tempfile directories default to 0700; services run as fleet.
            for directory, _, files in os.walk(staging, followlinks=False):
                Path(directory).chmod(0o755)
                for name in files:
                    path = Path(directory) / name
                    if not path.is_symlink():
                        path.chmod(0o644)
            staging.rename(release)
            return release
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def restart(self):
        self.command(self.cfg.get('restart_command',
                                 ['systemctl', 'restart', 'fleet-hub', 'fleet-mcp']), timeout=60)

    def healthy(self):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        health_url = self.cfg.get('health_url', 'http://127.0.0.1:8090/health')
        mcp_url = self.cfg.get('mcp_url', 'http://127.0.0.1:8080/mcp')
        with opener.open(health_url, timeout=3) as response:
            if json.load(response).get('ok') is not True:
                return False
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'MCP-Protocol-Version': '2025-11-25'}
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': '2025-11-25', 'capabilities': {},
                'clientInfo': {'name': 'fleet-updater-health', 'version': '1'}}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list', 'params': {}},
        ]
        for message in messages:
            request = urllib.request.Request(mcp_url, data=json.dumps(message).encode(), headers=headers)
            with opener.open(request, timeout=3) as response:
                payload = json.load(response)
            if 'error' in payload or 'result' not in payload:
                return False
            if message['method'] == 'tools/list':
                names = {tool['name'] for tool in payload['result'].get('tools', [])}
                if not {'ssh_exec', 'ssh_spawn', 'fleet_status', 'fleet_enroll', 'fleet_revoke'} <= names:
                    return False
        return True

    def wait_healthy(self):
        deadline = time.monotonic() + self.cfg.get('health_timeout', 30)
        while time.monotonic() < deadline:
            try:
                if self.healthy():
                    return True
            except UpdateInterrupted:
                raise
            except Exception:
                pass
            time.sleep(self.cfg.get('health_interval', 2))
        return False

    def run(self, retry=False):
        target = None
        previous = None
        activated = False
        prior_status = self.status.get('status')
        self.write_status('checking', 'Checking Git main', checked_at=now())
        try:
            if not self.current.is_symlink():
                raise RuntimeError('Bootstrap is required: current must point to the active release')
            previous = self.current.resolve(strict=True)
            target = self.fetch()
            current_commit = json.loads((previous / 'release.json').read_text()).get('commit', 'bootstrap')
            self.status.update(current_commit=current_commit, target_commit=target)
            if target == current_commit:
                self.write_status('idle', 'Already running the latest commit')
                return 0
            if not retry and target == self.status.get('last_failed_commit'):
                self.write_status(prior_status if prior_status in ('failed', 'rolled_back') else 'failed',
                                  'Waiting for a new commit after the previous failed update')
                return 0
            self.write_status('updating', 'Preparing ' + target[:12])
            release = self.prepare(target)
            prepared = json.loads((release / 'release.json').read_text())
            self.status.update(prepare_seconds=prepared['prepare_seconds'],
                               dependencies_reused=prepared['dependencies_reused'])
            activated = True
            switch(self.current, release)
            self.restart()
            if not self.wait_healthy():
                raise RuntimeError('New release failed hub/MCP health checks')
            self.write_status('success', 'Deployed ' + target[:12], current_commit=target,
                              previous_commit=current_commit, deployed_at=now(), last_failed_commit=None)
            try:
                self.prune(previous)
            except OSError as exc:
                print('Release cleanup skipped: ' + str(exc), flush=True)
            return 0
        except Exception as exc:
            if isinstance(exc, UpdateInterrupted):
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            message = str(exc)
            if activated and previous is not None:
                try:
                    switch(self.current, previous)
                    self.status['current_commit'] = json.loads((previous / 'release.json').read_text()).get('commit', 'bootstrap')
                    self.restart()
                    restored = self.wait_healthy()
                except Exception:
                    restored = False
                status = 'rolled_back' if restored else 'failed'
                message += '; previous release restored' if restored else '; rollback health check failed'
            else:
                status = 'failed'
            self.write_status(status, message, **({'last_failed_commit': target} if target else {}))
            return 1

    def prune(self, previous):
        keep = {self.current.resolve(), previous.resolve()}
        for path in self.releases.iterdir():
            if path.is_dir() and not path.is_symlink() and path.resolve() not in keep:
                if re.fullmatch(r'[a-f0-9]{40,64}|bootstrap-\d+', path.name):
                    shutil.rmtree(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/fleet-updater.json')
    parser.add_argument('--retry', action='store_true', help='Retry the last failed commit once')
    args = parser.parse_args()
    config_path = Path(args.config)
    config = json.loads(config_path.read_text())
    def interrupted(signum, frame):
        raise UpdateInterrupted('Update interrupted; restoring the previous release if activated')
    signal.signal(signal.SIGTERM, interrupted)
    # The timer and manual invocations share one nonblocking lock.
    import fcntl
    lock_path = Path(config.get('lock_path', '/var/lib/fleet-updater/update.lock'))
    with lock_path.open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        return Updater(config).run(retry=args.retry)


if __name__ == '__main__':
    raise SystemExit(main())
