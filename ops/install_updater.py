#!/usr/bin/env python3
"""One-time adoption of an existing /opt/fleet installation into Git releases."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from fleet_update import Updater, atomic_json, now, switch


def bootstrap(base: Path):
    """Preserve the installed app and downloads as the initial rollback release."""
    app = base / 'app'
    current = base / 'current'
    if current.is_symlink():
        if not app.is_symlink() or app.resolve() != (current / 'app').resolve():
            raise RuntimeError('Existing app/current links disagree; refusing to replace them')
        return current.resolve(strict=True)
    if current.exists() or not app.is_dir() or app.is_symlink():
        raise RuntimeError('Expected the existing app directory and no current release')
    releases = base / 'releases'
    releases.mkdir(mode=0o755, exist_ok=True)
    shared = base / 'shared'
    shared.mkdir(mode=0o755, exist_ok=True)
    downloads = app / 'downloads'
    shared_downloads = shared / 'downloads'
    if downloads.is_symlink():
        if downloads.resolve() != shared_downloads.resolve():
            raise RuntimeError('Existing downloads symlink is not the shared downloads directory')
    elif downloads.exists():
        if shared_downloads.exists():
            raise RuntimeError('Both app/downloads and shared/downloads exist; refusing to overwrite either')
        downloads.rename(shared_downloads)
        try:
            downloads.symlink_to(shared_downloads, target_is_directory=True)
        except BaseException:
            shared_downloads.rename(downloads)
            raise
    else:
        shared_downloads.mkdir(mode=0o755, exist_ok=True)
        downloads.symlink_to(shared_downloads, target_is_directory=True)
    release = releases / ('bootstrap-' + str(time.time_ns()))
    release.mkdir(mode=0o755)
    (release / 'vendor').mkdir(mode=0o755)
    atomic_json(release / 'release.json', {'commit': 'bootstrap', 'prepared_at': now()})
    app.rename(release / 'app')
    try:
        switch(current, release)
        switch(app, current / 'app')
    except BaseException:
        if app.is_symlink():
            app.unlink()
        (release / 'app').rename(app)
        current.unlink(missing_ok=True)
        raise
    # The services read these files as fleet; application code stays root-owned.
    for tree in (release, shared_downloads):
        for directory, dirs, files in os.walk(tree, followlinks=False):
            Path(directory).chmod(0o755)
            if os.geteuid() == 0:
                os.chown(directory, 0, 0)
            for name in dirs + files:
                path = Path(directory) / name
                if not path.is_symlink():
                    path.chmod(0o755 if path.is_dir() else 0o644)
                    if os.geteuid() == 0:
                        os.chown(path, 0, 0)
    return release


def command(*args):
    subprocess.run(args, check=True)


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run this installer as root on the existing Fleet hub')
    base = Path('/opt/fleet')
    source = Path(__file__).resolve().parent
    if not (base / 'repo' / '.git').exists():
        raise SystemExit('Clone the public Git repository to /opt/fleet/repo first')
    if not (base / 'venv' / 'bin' / 'python').exists():
        raise SystemExit('The existing Fleet Python environment was not found')
    bootstrap(base)
    ops = base / 'ops'
    ops.mkdir(mode=0o755, exist_ok=True)
    shutil.copyfile(source / 'fleet_update.py', ops / 'fleet_update.py')
    (ops / 'fleet_update.py').chmod(0o644)
    Path('/var/lib/fleet-updater').mkdir(mode=0o700, exist_ok=True)
    config_path = Path('/etc/fleet-updater.json')
    if not config_path.exists():
        shutil.copyfile(source / 'updater-config.example.json', config_path)
        config_path.chmod(0o644)
    for name in ('fleet-update.service', 'fleet-update.timer'):
        target = Path('/etc/systemd/system') / name
        shutil.copyfile(source / name, target)
        target.chmod(0o644)
    for name in ('fleet-hub', 'fleet-mcp'):
        directory = Path('/etc/systemd/system') / (name + '.service.d')
        directory.mkdir(mode=0o755, exist_ok=True)
        target = directory / 'releases.conf'
        target.write_text('[Service]\nEnvironment=PYTHONPATH=/opt/fleet/current/vendor\n')
        target.chmod(0o644)
    command('systemctl', 'daemon-reload')
    command('systemctl', 'restart', 'fleet-hub', 'fleet-mcp')
    updater = Updater(json.loads(config_path.read_text()))
    if not updater.wait_healthy():
        raise SystemExit('Bootstrap hub/MCP health check failed; timer has not been enabled')
    command('systemctl', 'start', 'fleet-update.service')
    command('systemctl', 'enable', '--now', 'fleet-update.timer')
    print('Fleet Git updates enabled: every 10 minutes. Runtime data preserved.')


if __name__ == '__main__':
    main()
