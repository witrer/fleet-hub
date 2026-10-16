"""Installer checks use a loopback hub and intercept every bootstrap download."""
import hashlib
import http.server
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import threading
from types import SimpleNamespace

import pytest


INSTALLER = Path(__file__).parent / 'static/install.sh'
UV_SHA = '6c86040a8f045ab9adc9e3ec4cdafa03fd00e9a988ed2bf28971c7fc2d0debd0'
SOURCE = b'''import json, os, pathlib, sys
pathlib.Path(os.environ['TEST_RESULT']).write_text(json.dumps(sys.argv[1:]))
with open(os.environ['TEST_LOG'], 'a') as output:
    output.write('{"installed": true}\\n')
'''


@pytest.fixture
def launcher(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    root = tmp_path / 'node'
    log = tmp_path / 'calls.jsonl'
    result = tmp_path / 'result.json'

    def executable(path, body):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(0o700)
        return path

    for name in ('mkdir', 'mktemp', 'rm', 'tar', 'gzip', 'sha256sum'):
        (bin_dir / name).symlink_to(shutil.which(name))
    executable(bin_dir / 'id', '#!/bin/bash\nprintf "%s\\n" "${TEST_UID:-0}"\n')
    executable(bin_dir / 'uname', '#!/bin/bash\nprintf "x86_64\\n"\n')
    # Unexpected package-manager use always fails locally, never touches the host.
    for name in ('apt-get', 'dnf', 'yum', 'apk'):
        executable(bin_dir / name, '#!/bin/bash\nprintf "package-manager\\n" >> "$TEST_LOG"\nexit 99\n')

    def python(path, label):
        return executable(path, '#!/bin/bash\nprintf \'{"python":"' + label + '"}\\n\' >> "$TEST_LOG"\n'
                          'exec "$TEST_PYTHON" "$@"\n')

    bootstrap_python = python(tmp_path / 'bootstrap-python', 'managed')
    fake_uv = b'''#!/bin/bash
printf '{"uv":"%s"}\\n' "$*" >> "$TEST_LOG"
if [[ "$1 $2" == 'python install' ]]; then
    /bin/mkdir -p "$UV_PYTHON_INSTALL_DIR/cpython-3.12-test/bin"
    /bin/cp "$TEST_BOOTSTRAP_PYTHON" "$UV_PYTHON_INSTALL_DIR/cpython-3.12-test/bin/python3"
elif [[ "$1 $2" == 'python find' ]]; then
    printf '%s\\n' "$UV_PYTHON_INSTALL_DIR/cpython-3.12-test/bin/python3"
else
    exit 99
fi
'''
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        member = tarfile.TarInfo('uv-x86_64-unknown-linux-musl/uv')
        member.size, member.mode = len(fake_uv), 0o700
        archive.addfile(member, io.BytesIO(fake_uv))
    archive_path = tmp_path / 'uv.tar.gz'
    archive_path.write_bytes(buffer.getvalue())
    release = SimpleNamespace(sources=[], checksums=[], requests=[])

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            release.requests.append(self.path)
            if self.path == '/node.py':
                payload = release.sources.pop(0) if release.sources else SOURCE
            elif self.path == '/api/agent-release':
                checksum = release.checksums.pop(0) if release.checksums else hashlib.sha256(SOURCE).hexdigest()
                payload = json.dumps({'sha256': checksum}).encode()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(payload)

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    hub = 'http://127.0.0.1:' + str(server.server_port)
    # The wrapper serves synthetic uv bytes and only permits the loopback node
    # request to invoke curl. Mirror, fallback and package operations stay local.
    executable(bin_dir / 'curl', '#!' + sys.executable + '\n' + '''
import json, os, pathlib, shutil, subprocess, sys
args = sys.argv[1:]
url = next(arg for arg in args if arg.startswith(('http://', 'https://')))
with open(os.environ['TEST_LOG'], 'a') as output:
    output.write(json.dumps({'curl': args, 'url': url}) + '\\n')
destination = pathlib.Path(args[args.index('-o') + 1])
if '/downloads/uv-' in url or url.startswith('https://gh-proxy.org/'):
    if url.startswith('https://gh-proxy.org/') and os.environ.get('TEST_MIRROR_FAIL'):
        destination.write_bytes(b'incomplete archive')
        sys.exit(28)
    if os.environ.get('TEST_CORRUPT_ARCHIVE'):
        destination.write_bytes(b'corrupt archive')
    else:
        shutil.copyfile(os.environ['TEST_ARCHIVE'], destination)
elif url == os.environ['FLEET_HUB'] + '/node.py':
    sys.exit(subprocess.call([os.environ['TEST_CURL'], *args]))
else:
    raise SystemExit('Unexpected network access')
''')
    env = {'PATH': str(bin_dir), 'FLEET_NODE_DIR': str(root), 'FLEET_HUB': hub,
           'FLEET_PYTHON': 'missing-test-python', 'TEST_LOG': str(log),
           'TEST_RESULT': str(result), 'TEST_PYTHON': sys.executable,
           'TEST_BOOTSTRAP_PYTHON': str(bootstrap_python), 'TEST_CURL': shutil.which('curl'),
           'TEST_ARCHIVE': str(archive_path), 'NO_PROXY': '127.0.0.1,localhost'}

    def run(**overrides):
        # Use the pinned hash of our synthetic archive; production's pinned
        # version and checksum are unchanged.
        source = INSTALLER.read_text().replace(UV_SHA, hashlib.sha256(buffer.getvalue()).hexdigest())
        return subprocess.run(['/bin/bash', '-s', '--', 'synthetic-token'], input=source,
                              text=True, env={**env, **overrides}, capture_output=True, timeout=15)

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    try:
        yield SimpleNamespace(root=root, bin=bin_dir, python=python, run=run, calls=calls,
                              result=result, release=release, uv=fake_uv, executable=executable)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_managed_python_is_reused_before_root_package_install(launcher):
    launcher.python(launcher.root / 'runtime/python/cpython-3.12-test/bin/python3', 'cached')
    launcher.executable(launcher.root / 'runtime/uv', '#!/bin/bash\nexit 99\n')
    result = launcher.run()
    assert result.returncode == 0, result.stderr
    assert {item['python'] for item in launcher.calls() if 'python' in item} == {'cached'}
    assert len([item for item in launcher.calls() if 'curl' in item]) == 1
    assert json.loads(launcher.result.read_text())[-1] == '--token=synthetic-token'
    assert not list(launcher.root.glob('node-install.*'))


def test_explicit_python_keeps_priority_over_system_and_managed(launcher):
    explicit = launcher.python(launcher.root / 'explicit-python', 'explicit')
    launcher.python(launcher.bin / 'python3', 'system')
    launcher.python(launcher.root / 'runtime/python/cpython-3.12-test/bin/python3', 'cached')
    result = launcher.run(FLEET_PYTHON=str(explicit))
    assert result.returncode == 0, result.stderr
    assert {item['python'] for item in launcher.calls() if 'python' in item} == {'explicit'}


def test_unusable_cached_python_does_not_prevent_bootstrap(launcher):
    launcher.executable(launcher.root / 'runtime/python/cpython-old/bin/python3', '#!/bin/bash\nexit 1\n')
    result = launcher.run(TEST_UID='1000')
    assert result.returncode == 0, result.stderr
    assert launcher.result.exists()
    assert any(item.get('uv', '').startswith('python install ') for item in launcher.calls())


@pytest.mark.parametrize('mirror_fails', [False, True])
def test_uv_bootstrap_uses_bounded_downloads_and_only_falls_back_on_failure(launcher, mirror_fails):
    result = launcher.run(TEST_UID='1000', TEST_MIRROR_FAIL='1' if mirror_fails else '')
    assert result.returncode == 0, result.stderr
    downloads = [item for item in launcher.calls() if 'curl' in item]
    assert downloads[0]['url'].startswith('https://gh-proxy.org/')
    assert len(downloads) == (3 if mirror_fails else 2)
    if mirror_fails:
        assert '/downloads/uv-0.13.1-' in downloads[1]['url']
    for download in downloads:
        args = download['curl']
        assert args[args.index('--connect-timeout') + 1] == '10'
        assert args[args.index('--speed-limit') + 1] == '1'
        assert args[args.index('--speed-time') + 1] == '30'
        assert args[args.index('--retry-max-time') + 1] == '60'
        assert '--max-time' not in args
    assert launcher.result.exists()


def test_corrupt_uv_archive_is_rejected_and_temporary_file_removed(launcher):
    result = launcher.run(TEST_UID='1000', TEST_CORRUPT_ARCHIVE='1')
    assert result.returncode != 0
    assert not launcher.result.exists()
    assert not (launcher.root / 'runtime/uv').exists()
    assert not list((launcher.root / 'runtime').glob('uv.*'))
    assert not any('uv' in item for item in launcher.calls())


def test_changed_release_is_downloaded_again_and_installed_once(launcher):
    launcher.python(launcher.bin / 'python3', 'system')
    launcher.release.sources = [SOURCE + b'# previous release\n', SOURCE]
    result = launcher.run()
    assert result.returncode == 0, result.stderr
    assert launcher.release.requests == ['/node.py', '/api/agent-release'] * 2
    assert sum(item.get('installed', False) for item in launcher.calls()) == 1
    assert launcher.result.exists()
    assert not list(launcher.root.glob('node-install.*'))


def test_repeated_checksum_mismatch_fails_without_execution_and_cleans_temp(launcher):
    launcher.python(launcher.bin / 'python3', 'system')
    launcher.release.sources = [SOURCE + b'# previous release\n'] * 2
    result = launcher.run()
    assert result.returncode == 75
    assert '节点程序下载校验失败' in result.stderr
    assert launcher.release.requests == ['/node.py', '/api/agent-release'] * 2
    assert not any(item.get('installed') for item in launcher.calls())
    assert not launcher.result.exists()
    assert not list(launcher.root.glob('node-install.*'))


def test_compile_failure_is_not_retried_or_executed(launcher):
    launcher.python(launcher.bin / 'python3', 'system')
    invalid_source = b'not valid python syntax\n'
    launcher.release.sources = [invalid_source]
    launcher.release.checksums = [hashlib.sha256(invalid_source).hexdigest()]
    result = launcher.run()
    assert result.returncode == 1
    assert 'SyntaxError' in result.stderr
    assert launcher.release.requests == ['/node.py', '/api/agent-release']
    assert not any(item.get('installed') for item in launcher.calls())
    assert not launcher.result.exists()
    assert not list(launcher.root.glob('node-install.*'))
