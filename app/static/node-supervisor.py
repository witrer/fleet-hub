#!/usr/bin/env python3
"""Stable, standard-library-only guard for independently replaceable Fleet agents."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import time

PROBATION_SECONDS = 60
STARTUP_TIMEOUT = 90
LOOP_MAX_AGE = 35
RESTART_DELAY = 2


def atomic(path, content):
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('wb') as output:
        output.write(content.encode() if isinstance(content, str) else content)
        output.flush()
        os.fsync(output.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state(root):
    try:
        value = json.loads((root / 'agent-update.json').read_text())
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}


def save(root, value):
    atomic(root / 'agent-update.json', json.dumps(value))


@contextlib.contextmanager
def lock(root, name):
    with (root / (name + '.lock')).open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Fleet ' + name + ' 已在运行')
        yield


def stop(child):
    if child is not None and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)


def spawn(root, ready_fd=None):
    # Keep the parent-death guarantee across the guard -> agent -> FRP chain.
    bootstrap = '''import ctypes, os, signal, sys
if ctypes.CDLL(None).prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
    sys.exit(1)
if os.getppid() != int(sys.argv[1]):
    sys.exit(1)
os.execv(sys.argv[2], sys.argv[2:])
'''
    environment = dict(os.environ)
    environment.pop('FLEET_AGENT_READY_FD', None)
    if ready_fd is not None:
        environment['FLEET_AGENT_READY_FD'] = str(ready_fd)
    return subprocess.Popen([sys.executable, '-I', '-S', '-c', bootstrap, str(os.getpid()),
                             sys.executable, str(root / 'node-agent.py'), 'run', '--root', str(root)],
                            env=environment, pass_fds=() if ready_fd is None else (ready_fd,))


def rollback(root, pending, reason):
    backup = root / 'node-agent.previous.py'
    if digest(backup) != pending['previous_sha256']:
        raise RuntimeError('Agent rollback backup checksum mismatch')
    # Persist the decision before restoring source. A crash between the restore
    # and rejection marker must not let the same broken release install again.
    current = state(root)
    current['pending'] = dict(pending, rollback_reason=reason)
    save(root, current)
    atomic(root / 'node-agent.py', backup.read_bytes())
    current = state(root)
    rejected = [item for item in current.get('rejected', []) if item != pending['sha256']]
    current.update(pending=None, rejected=(rejected + [pending['sha256']])[-8:],
                   last_result='rolled_back', last_version=pending.get('version'),
                   reason=reason, updated_at=time.time())
    save(root, current)
    print('节点更新未通过本地运行检查，已恢复上一版本：' + reason, flush=True)


def supervise(root, action):
    with contextlib.ExitStack() as stack:
        stack.enter_context(lock(root, 'guard'))
        if action == 'supervise':
            stack.enter_context(lock(root, 'supervise'))
            atomic(root / 'supervise.pid', str(os.getpid()))
        atomic(root / 'guard.pid', str(os.getpid()))
        child = None
        ready_read = None

        def shutdown(*_):
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        try:
            while True:
                current = state(root)
                pending = current.get('pending')
                if pending and pending.get('rollback_reason'):
                    rollback(root, pending, pending['rollback_reason'])
                    pending = None
                elif pending and digest(root / 'node-agent.py') != pending['sha256']:
                    # An interrupted write may have left the previous source installed.
                    if digest(root / 'node-agent.py') != pending['previous_sha256']:
                        rollback(root, pending, 'candidate_checksum')
                    else:
                        current['pending'] = None
                        save(root, current)
                    pending = None
                ready_write = None
                if pending:
                    ready_read, ready_write = os.pipe()
                    os.set_blocking(ready_read, False)
                try:
                    child = spawn(root, ready_write)
                finally:
                    if ready_write is not None:
                        os.close(ready_write)
                started = time.monotonic()
                last_ready = None
                while child.poll() is None:
                    now = time.monotonic()
                    if pending:
                        with contextlib.suppress(BlockingIOError):
                            readiness = os.read(ready_read, 4096)
                            if readiness:
                                last_ready = now if readiness[-1:] == b'.' else None
                        if now - started >= PROBATION_SECONDS and last_ready is not None and now - last_ready <= LOOP_MAX_AGE:
                            current = state(root)
                            current.update(pending=None, last_result='confirmed',
                                           last_version=pending.get('version'), reason=None, updated_at=time.time())
                            save(root, current)
                            pending = None
                            os.close(ready_read)
                            ready_read = None
                        elif now - started >= STARTUP_TIMEOUT:
                            stop(child)
                            rollback(root, pending, 'startup_timeout')
                            pending = None
                    time.sleep(.5)
                if pending:
                    rollback(root, pending, 'early_exit_' + str(child.returncode))
                if ready_read is not None:
                    os.close(ready_read)
                    ready_read = None
                time.sleep(RESTART_DELAY)
        finally:
            stop(child)
            if ready_read is not None:
                os.close(ready_read)
            (root / 'guard.pid').unlink(missing_ok=True)
            if action == 'supervise':
                (root / 'supervise.pid').unlink(missing_ok=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('run', 'supervise'))
    parser.add_argument('--root', required=True)
    arguments = parser.parse_args()
    supervise(pathlib.Path(arguments.root).resolve(), arguments.action)
