#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["paramiko>=3.4,<5"]
# ///
"""Upload files through an frp XTCP direct tunnel. No relay fallback or resume.

Run: FLEET_TOKEN=... uv run transfer.py --hub https://hub --node NODE \
     --source ./dataset --destination /data/dataset
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import logging
import os
import platform
import posixpath
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def api(hub: str, path: str, token: str, payload: dict, timeout: float = 15) -> dict:
    request = urllib.request.Request(
        hub.rstrip("/") + path,
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as exc:
        body = exc.read(2000).decode(errors="replace")
        raise RuntimeError(f"Hub returned HTTP {exc.code}: {body}") from None


def platform_name() -> str:
    operating_system = {"Windows": "windows", "Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    architecture = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    name = f"{operating_system}_{architecture}"
    if name not in {"windows_amd64", "linux_amd64", "linux_arm64", "darwin_amd64", "darwin_arm64"}:
        raise RuntimeError(f"Unsupported platform: {platform.system()} {platform.machine()}")
    return name


def collect_files(source: Path) -> tuple[list[tuple[Path, str, int]], list[str], int]:
    if source.is_symlink():
        raise RuntimeError("Source must be a regular file or directory, not a symbolic link")
    if source.is_file():
        size = source.stat().st_size
        return [(source, source.name, size)], [], size
    if not source.is_dir():
        raise RuntimeError(f"Source does not exist: {source}")
    files, directories = [], [""]
    for directory, dirnames, filenames in os.walk(source, followlinks=False):
        folder = Path(directory)
        for name in dirnames:
            child = folder / name
            if child.is_symlink():
                raise RuntimeError(f"Symbolic links are not uploaded: {child}")
            directories.append(child.relative_to(source).as_posix())
        for name in filenames:
            child = folder / name
            if child.is_symlink() or not child.is_file():
                raise RuntimeError(f"Only regular files are uploaded: {child}")
            files.append((child, child.relative_to(source).as_posix(), child.stat().st_size))
    files.sort(key=lambda entry: entry[1])
    directories.sort(key=lambda entry: (entry.count("/"), entry))
    return files, directories, sum(entry[2] for entry in files)


def frpc_binary(job: dict, system: str) -> Path:
    version = str(job["frp_version"]).removeprefix("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise RuntimeError("Hub supplied an invalid frp version")
    checksum = str(job["sha256"]).lower()
    if not re.fullmatch(r"[a-f0-9]{64}", checksum):
        raise RuntimeError("Hub supplied an invalid frp checksum")
    cache_root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".cache"))) / "fleet-hub" / "frp"
    cache = cache_root / f"{version}-{system}-{checksum[:12]}"
    filename = "frpc.exe" if system.startswith("windows") else "frpc"
    binary = cache / filename
    marker = cache / "binary.sha256"
    if binary.is_file() and marker.is_file():
        if hashlib.sha256(binary.read_bytes()).hexdigest() == marker.read_text().strip():
            return binary
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="download-", dir=cache) as scratch:
        archive = Path(scratch) / "archive"
        digest = hashlib.sha256()
        with urllib.request.urlopen(job["download_url"], timeout=45) as response, archive.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != checksum:
            raise RuntimeError("Downloaded frp archive checksum does not match; nothing was executed")
        extracted = Path(scratch) / filename
        if system.startswith("windows"):
            with zipfile.ZipFile(archive) as package:
                matches = [item for item in package.infolist() if not item.is_dir() and posixpath.basename(item.filename) == filename]
                if len(matches) != 1:
                    raise RuntimeError("frp archive does not contain exactly one frpc executable")
                with package.open(matches[0]) as source, extracted.open("wb") as output:
                    shutil.copyfileobj(source, output)
        else:
            with tarfile.open(archive, "r:gz") as package:
                matches = [item for item in package.getmembers() if item.isfile() and posixpath.basename(item.name) == filename]
                if len(matches) != 1:
                    raise RuntimeError("frp archive does not contain exactly one frpc executable")
                with package.extractfile(matches[0]) as source, extracted.open("wb") as output:
                    shutil.copyfileobj(source, output)
            extracted.chmod(0o700)
        digest_text = hashlib.sha256(extracted.read_bytes()).hexdigest()
        os.replace(extracted, binary)
        marker.write_text(digest_text, encoding="ascii")
    return binary


def free_port() -> int:
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        return candidate.getsockname()[1]


def render_config(template: str, ca_path: Path, port: int) -> str:
    if re.search(r"(?m)^\s*fallbackTo\s*=\s*[\"']\s*[^\"']", template):
        raise RuntimeError("Hub returned a relay fallback; direct transfers require no fallbackTo")
    if not re.search(r"(?m)^\s*type\s*=\s*[\"']xtcp[\"']\s*$", template):
        raise RuntimeError("Hub did not return an XTCP visitor configuration")
    template, count = re.subn(r"(?m)^(\s*bindPort\s*=\s*)0\s*$", lambda match: match[1] + str(port), template)
    if count != 1:
        raise RuntimeError("Expected exactly one bindPort = 0 in visitor configuration")
    # Normalize the path even when the server sends another placeholder. JSON
    # strings are valid TOML basic strings, including Windows backslash escapes.
    ca_line = "transport.tls.trustedCaFile = " + json.dumps(str(ca_path.resolve()))
    template, ca_count = re.subn(r"(?m)^\s*transport\.tls\.trustedCaFile\s*=.*$", lambda _: ca_line, template)
    if ca_count != 1:
        raise RuntimeError("Expected exactly one transport.tls.trustedCaFile in visitor configuration")
    return template


class Progress:
    def __init__(self, hub: str, job: dict, total: int):
        self.hub, self.job, self.total = hub, job, total
        self.bytes_done, self.status, self.error = 0, "connecting", ""
        self.phase = "preparing_tool"
        self.started = time.monotonic()
        self.transfer_started = self.finished = None
        self.stop_event, self.lock = threading.Event(), threading.Lock()
        self.thread = threading.Thread(target=self.worker, daemon=True)

    def payload(self) -> dict:
        with self.lock:
            now = self.finished if self.finished is not None else time.monotonic()
            transfer_seconds = max(0, now - self.transfer_started) if self.transfer_started is not None else 0
            speed = self.bytes_done / max(transfer_seconds, 0.001) if self.transfer_started is not None else 0
            return {"bytes_done": self.bytes_done, "speed_bps": speed, "status": self.status, "error": self.error,
                    "phase": self.phase, "elapsed_seconds": max(0, now - self.started), "transfer_seconds": transfer_seconds}

    def send(self) -> None:
        api(self.hub, f"/api/transfers/{urllib.parse.quote(str(self.job['id']), safe='')}/progress", self.job["token"], self.payload(), timeout=4)

    def worker(self) -> None:
        while not self.stop_event.wait(1):
            try:
                self.send()
            except Exception:
                # A transient monitoring failure must not redirect or corrupt data.
                pass

    def update(self, *, completed: int | None = None, status: str | None = None, phase: str | None = None) -> None:
        with self.lock:
            if completed is not None:
                self.bytes_done = completed
            if status is not None:
                self.status = status
                self.phase = status
                if status == "transferring" and self.transfer_started is None:
                    self.transfer_started = time.monotonic()
            if phase is not None:
                self.phase = phase

    def finish(self, status: str, error: str = "") -> None:
        self.stop_event.set()
        with self.lock:
            if self.finished is None:
                self.finished = time.monotonic()
            self.error = (f"{self.phase}: {error}" if error else "")[:1500]
            self.status = self.phase = status
        if self.thread.is_alive():
            self.thread.join(timeout=6)
        for attempt in range(3):
            try:
                self.send()
                return
            except Exception as exc:
                if attempt == 2:
                    print(f"Could not send final transfer status: {exc}", file=sys.stderr)
                else:
                    time.sleep(0.5)


def host_key(value: str) -> paramiko.PKey:
    parts = value.strip().split()
    if len(parts) < 2:
        raise RuntimeError("Hub did not provide an SSH host public key")
    key_type, key_data = parts[:2]
    if not key_type.startswith(("ssh-", "ecdsa-")):
        raise RuntimeError("Expected SSH host key in 'keytype base64' format")
    return paramiko.PKey.from_type_string(key_type, base64.b64decode(key_data, validate=True))


def connect_direct(job: dict, key: paramiko.PKey, port: int, process: subprocess.Popen, log_path: Path, connect_timeout: int = 180) -> paramiko.SSHClient:
    pinned_key = host_key(job["ssh_host_key"])
    deadline = time.monotonic() + connect_timeout
    last_error = "direct tunnel is not ready"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log = log_path.read_text(errors="replace")[-1800:]
            raise RuntimeError("frpc exited: " + log)
        client = paramiko.SSHClient()
        client.get_host_keys().add(f"[127.0.0.1]:{port}", pinned_key.get_name(), pinned_key)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            remaining = max(0.1, deadline - time.monotonic())
            client.connect("127.0.0.1", port=port, username=job["ssh_user"], pkey=key,
                           timeout=min(5, remaining), banner_timeout=min(15, remaining), auth_timeout=min(15, remaining),
                           allow_agent=False, look_for_keys=False)
            client.get_transport().set_keepalive(10)
            return client
        except paramiko.AuthenticationException as exc:
            client.close()
            raise RuntimeError("Direct tunnel reached SSH, but SSH authentication failed: " + str(exc)) from exc
        except paramiko.BadHostKeyException as exc:
            client.close()
            raise RuntimeError("Direct tunnel reached SSH, but the SSH host key did not match") from exc
        except (OSError, EOFError, paramiko.SSHException) as exc:
            last_error = str(exc)
            client.close()
            time.sleep(0.3)
    log = log_path.read_text(errors="replace")
    stage = "SSH handshake over the direct tunnel" if "establishing nat hole connection successful" in log else "P2P UDP hole punching"
    raise RuntimeError(f"Timed out after {connect_timeout} seconds waiting for {stage}; no relay fallback was used. " + last_error)


def stat_optional(sftp: paramiko.SFTPClient, path: str):
    try:
        return sftp.lstat(path)
    except OSError as exc:
        if exc.errno == 2:
            return None
        raise


def ensure_directory(sftp: paramiko.SFTPClient, path: str) -> None:
    if path in ("", ".", "/"):
        return
    existing = stat_optional(sftp, path)
    if existing is not None:
        if not stat.S_ISDIR(sftp.stat(path).st_mode):
            raise RuntimeError(f"Remote path is not a directory: {path}")
        return
    ensure_directory(sftp, posixpath.dirname(path))
    sftp.mkdir(path)


def upload(client: paramiko.SSHClient, job: dict, source: Path, files: list, directories: list[str], destination: str, overwrite: bool, progress: Progress) -> None:
    with client.open_sftp() as sftp:
        sftp.get_channel().settimeout(60)
        if source.is_dir():
            pairs = [(local, posixpath.join(destination, relative), size) for local, relative, size in files]
            remote_directories = [posixpath.join(destination, relative) for relative in directories]
        else:
            existing = stat_optional(sftp, destination)
            target = posixpath.join(destination, source.name) if existing is not None and stat.S_ISDIR(sftp.stat(destination).st_mode) else destination
            pairs = [(files[0][0], target, files[0][2])]
            remote_directories = [posixpath.dirname(target)]
        for _, target, _ in pairs:
            existing = stat_optional(sftp, target)
            if existing is not None:
                if not overwrite:
                    raise RuntimeError(f"Remote file already exists: {target}; pass --overwrite to replace it")
                if stat.S_ISDIR(existing.st_mode):
                    raise RuntimeError(f"Cannot replace a directory with a file: {target}")
        for directory in remote_directories:
            ensure_directory(sftp, directory)
        completed = 0
        progress.update(status="transferring")
        for local, target, expected_size in pairs:
            if local.stat().st_size != expected_size:
                raise RuntimeError(f"Source file changed during transfer preparation: {local}")
            temporary = target + ".fleet-part-" + re.sub(r"[^A-Za-z0-9_-]", "_", str(job["id"]))
            if stat_optional(sftp, temporary) is not None:
                raise RuntimeError(f"Temporary remote upload already exists: {temporary}")
            temporary_created = False
            try:
                # Exclusive open prevents two upload jobs from sharing a temporary file.
                with local.open("rb") as incoming, sftp.open(temporary, "wx") as outgoing:
                    temporary_created = True
                    outgoing.set_pipelined(True)
                    transferred = 0
                    while chunk := incoming.read(1024 * 1024):
                        outgoing.write(chunk)
                        transferred += len(chunk)
                        progress.update(completed=completed + transferred)
                if transferred != expected_size or sftp.stat(temporary).st_size != expected_size:
                    raise RuntimeError(f"File size changed or upload was incomplete: {local}")
                if overwrite:
                    # OpenSSH's POSIX rename is atomic and supports replacement.
                    sftp.posix_rename(temporary, target)
                else:
                    # Standard SFTP rename refuses an existing destination.
                    sftp.rename(temporary, target)
                completed += transferred
                progress.update(completed=completed)
                print(f"Uploaded {target} ({transferred:,} bytes)", flush=True)
            except BaseException:
                if temporary_created:
                    try:
                        sftp.remove(temporary)
                    except OSError:
                        pass
                raise


def main() -> int:
    # Retry failures are reported once below, rather than a Paramiko traceback for
    # each SSH connection attempted while NAT traversal is still in progress.
    logging.getLogger("paramiko").setLevel(logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub", required=True)
    parser.add_argument("--node", required=True)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--destination", required=True, help="Remote POSIX path; directory sources are copied into this path")
    parser.add_argument("--connect-timeout", type=int, default=180, help="Seconds to allow P2P/SSH connection attempts (default: 180)")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.connect_timeout <= 0:
        parser.error("--connect-timeout must be a positive number of seconds")
    token = os.environ.get("FLEET_TOKEN", "")
    if not token:
        parser.error("Set the FLEET_TOKEN environment variable to your hub access token")
    if not args.destination or "\x00" in args.destination:
        parser.error("--destination must be a nonempty remote POSIX path")
    process = client = progress = None
    try:
        source = args.source.expanduser().absolute()
        files, directories, total = collect_files(source)
        system = platform_name()
        generated = Ed25519PrivateKey.generate()
        public_key = generated.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
        private_key = generated.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()).decode()
        ssh_key = paramiko.Ed25519Key.from_private_key(io.StringIO(private_key))
        # The hub allows up to 40 seconds for SSH authorization preparation.
        job = api(args.hub, "/api/transfers", token, {"node_id": args.node, "name": source.name, "bytes_total": total, "public_key": public_key, "platform": system}, timeout=45)
        progress = Progress(args.hub, job, total)
        progress.thread.start()
        print(f"Transfer {job['id']}: {len(files)} files, {total:,} bytes. P2P only; no relay fallback.", flush=True)
        binary = frpc_binary(job, system)
        with tempfile.TemporaryDirectory(prefix="fleet-transfer-") as scratch:
            scratch_path = Path(scratch)
            ca = scratch_path / "ca.crt"
            ca.write_text(job["ca_pem"], encoding="utf-8")
            port = free_port()
            config = scratch_path / "frpc.toml"
            config.write_text(render_config(job["frpc_config"], ca, port), encoding="utf-8")
            if os.name != "nt":
                config.chmod(0o600)
            log_path = scratch_path / "frpc.log"
            with log_path.open("wb") as log:
                process = subprocess.Popen([str(binary), "-c", str(config)], stdout=log, stderr=subprocess.STDOUT,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                try:
                    progress.update(phase="connecting")
                    print(f"Waiting up to {args.connect_timeout} seconds for P2P/SSH connection.", flush=True)
                    client = connect_direct(job, ssh_key, port, process, log_path, args.connect_timeout)
                    print("Direct SSH connection established; uploading.", flush=True)
                    upload(client, job, source, files, directories, args.destination, args.overwrite, progress)
                    progress.finish("completed")
                    print("Upload completed.", flush=True)
                finally:
                    if client is not None:
                        client.close()
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)
                    log.flush()
                    details = log_path.read_text(errors="replace")[-16000:]
                    for credential in (token, job["token"], *re.findall(r'(?m)^\s*secretKey\s*=\s*"([^"]+)"', job["frpc_config"])):
                        details = details.replace(credential, "[REDACTED]")
                    saved_logs = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".cache"))) / "fleet-hub" / "transfer-logs"
                    saved_logs.mkdir(parents=True, exist_ok=True)
                    saved_log = saved_logs / (re.sub(r"[^A-Za-z0-9_-]", "_", job["id"]) + ".log")
                    saved_log.write_text(details, encoding="utf-8")
                    if os.name != "nt":
                        saved_log.chmod(0o600)
                    print(f"FRP diagnostics: {saved_log}", flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        message = "Transfer interrupted" if isinstance(exc, KeyboardInterrupt) else str(exc)
        if progress is not None:
            progress.finish("failed", message)
        print(message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
