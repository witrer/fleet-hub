#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["paramiko>=3.4,<5"]
# ///
"""Upload through an frp XTCP tunnel or an explicitly selected OneDrive relay.

Run: FLEET_TOKEN=... uv run transfer.py --hub https://hub --node NODE \
     --source ./dataset --destination /data/dataset
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import http.client
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
import uuid
import zipfile
from pathlib import Path

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


RELAY_CHUNK_SIZE = 10 * 1024 * 1024  # A multiple of Graph's 320 KiB requirement.
RELAY_ATTEMPTS = 5


class RelayHTTPError(RuntimeError):
    def __init__(self, service: str, status: int, retry_after: float = 0):
        super().__init__(f"{service} returned HTTP {status}")
        self.status, self.retry_after = status, retry_after


class RelayUncertainError(RuntimeError):
    """The response was lost or unusable; query the server before retrying."""


class RelayNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # Never forward the hub's bearer token or a signed PUT to another host.
        return None


def relay_http(request: urllib.request.Request, service: str, timeout: float = 45) -> tuple[int, dict]:
    try:
        with urllib.request.build_opener(RelayNoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise RelayUncertainError(f"{service} returned an oversized response")
            try:
                data = json.loads(raw) if raw else {}
            except (ValueError, UnicodeDecodeError):
                raise RelayUncertainError(f"{service} returned invalid JSON") from None
            if not isinstance(data, dict):
                raise RelayUncertainError(f"{service} returned an invalid response")
            return response.status, data
    except urllib.error.HTTPError as exc:
        # Graph URLs and response bodies may contain upload capabilities.
        try:
            retry_after = min(60, max(0, float(exc.headers.get("Retry-After", "0"))))
        except (TypeError, ValueError):
            retry_after = 0
        exc.close()
        raise RelayHTTPError(service, exc.code, retry_after) from None
    except (urllib.error.URLError, OSError, EOFError, http.client.HTTPException):
        raise RelayUncertainError(f"{service} connection failed; resumable state was retained") from None


def relay_api(hub: str, path: str, token: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(hub.rstrip("/") + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST" if payload is not None else "GET")
    for attempt in range(RELAY_ATTEMPTS):
        try:
            return relay_http(request, "Hub")[1]
        except RelayHTTPError as exc:
            if exc.status != 429 and exc.status < 500 or attempt == RELAY_ATTEMPTS - 1:
                raise
            delay = max(exc.retry_after, min(2 ** attempt, 16))
        except RelayUncertainError:
            if attempt == RELAY_ATTEMPTS - 1:
                raise
            delay = min(2 ** attempt, 16)
        time.sleep(delay)
    raise RuntimeError("Hub retries exhausted")


def relay_manifest(files: list, directories: list[str]) -> dict:
    entries = []
    for local, relative, size in files:
        current = local.stat()
        if local.is_symlink() or not stat.S_ISREG(current.st_mode) or current.st_size != size:
            raise RuntimeError("Source changed while preparing the relay manifest")
        entries.append({"path": relative, "size": size, "mtime_ns": current.st_mtime_ns})
    return {"files": entries, "directories": directories}


def relay_cache_path(context: dict) -> Path:
    default = (Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "fleet-hub" / "client"
               if os.name == "nt" else Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "fleet-hub" / "client")
    root = Path(os.environ.get("FLEET_CLIENT_HOME", str(default))).expanduser()
    if root.is_symlink():
        raise RuntimeError("Client directory must not be a symbolic link")
    cache = root / "relay-transfers"
    if cache.is_symlink():
        raise RuntimeError("Relay cache must not be a symbolic link")
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    return cache / (key + ".json")


@contextlib.contextmanager
def relay_state_lock(path: Path):
    lock_path = path.with_suffix(".lock")
    if lock_path.is_symlink():
        raise RuntimeError("Relay lock must not be a symbolic link")
    with lock_path.open("a+b") as handle:
        if os.name != "nt":
            import fcntl
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError("Another uploader is using this source and destination") from None
        else:
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError("Another uploader is using this source and destination") from None
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def save_relay_state(path: Path, state: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".relay-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(state, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def relay_upload_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        valid = (parsed.scheme == "https" or parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"})
        if not valid or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError
    except (ValueError, TypeError):
        raise RuntimeError("Hub returned an invalid upload URL") from None
    return value


def relay_offset(value, size: int) -> int:
    if type(value) is not int or value < 0 or value > size or value != size and value % (320 * 1024):
        raise RuntimeError("OneDrive returned an invalid resume offset")
    return value


def relay_complete(hub: str, prefix: str, token: str, item_id: str | None = None, probe: bool = False) -> bool:
    try:
        result = relay_api(hub, prefix + "/complete", token, {"item_id": item_id} if item_id else {})
    except RelayHTTPError as exc:
        if probe and exc.status == 409:
            return False
        raise
    if result.get("uploaded") is True:
        return True
    if probe and result.get("uploaded") is False:
        return False
    raise RuntimeError("Hub did not confirm the OneDrive upload")


def upload_relay_file(hub: str, token: str, task_id: str, entry: dict, local: Path, expected: dict) -> None:
    prefix = "/api/relay/transfers/" + urllib.parse.quote(task_id, safe="") + "/files/" + urllib.parse.quote(str(entry["id"]), safe="")
    size, expected_mtime = expected["size"], expected["mtime_ns"]

    def check_source(handle=None):
        current = os.fstat(handle.fileno()) if handle else local.stat()
        if local.is_symlink() or not stat.S_ISREG(current.st_mode) or (current.st_size, current.st_mtime_ns) != (size, expected_mtime):
            raise RuntimeError("Source changed; this relay task cannot safely resume")

    check_source()
    session = relay_api(hub, prefix + "/session", token, {})
    failures = restarts = 0
    last_print = time.monotonic()
    with local.open("rb") as incoming:
        while True:
            check_source(incoming)
            if session.get("uploaded") is True:
                return
            offset = relay_offset(session.get("next_offset", 0), size)
            if offset == size:
                relay_complete(hub, prefix, token)
                return
            upload_url = relay_upload_url(session.get("upload_url"))
            incoming.seek(offset)
            chunk = incoming.read(min(RELAY_CHUNK_SIZE, size - offset))
            check_source(incoming)
            if len(chunk) != min(RELAY_CHUNK_SIZE, size - offset):
                raise RuntimeError("Source changed during the relay upload")
            end = offset + len(chunk)
            request = urllib.request.Request(upload_url, data=chunk, method="PUT", headers={
                "Content-Type": "application/octet-stream", "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {offset}-{end - 1}/{size}",
            })
            retry_after = 0
            try:
                status, result = relay_http(request, "OneDrive", timeout=90)
            except RelayHTTPError as exc:
                if exc.status not in {401, 403, 404, 408, 410, 416, 429} and exc.status < 500:
                    raise
                retry_after = exc.retry_after
            except RelayUncertainError:
                # Reconcile ambiguous replies before sending any bytes again.
                pass
            else:
                if status in (200, 201):
                    check_source(incoming)
                    relay_complete(hub, prefix, token, result.get("id") if isinstance(result.get("id"), str) else None)
                    return
                if status != 202:
                    raise RuntimeError("OneDrive returned an unexpected upload response")
                ranges = result.get("nextExpectedRanges")
                if not isinstance(ranges, list) or not ranges or not all(isinstance(value, str) and re.fullmatch(r"\d+-\d*", value) for value in ranges):
                    raise RuntimeError("OneDrive returned invalid upload ranges")
                next_offset = relay_offset(min(int(value.split("-", 1)[0]) for value in ranges), size)
                if next_offset <= offset:
                    raise RuntimeError("OneDrive upload did not advance")
                session["next_offset"] = next_offset
                failures = 0
                if time.monotonic() - last_print >= 5:
                    print(f"OneDrive upload: {expected['path']} ({next_offset:,}/{size:,} bytes)", flush=True)
                    last_print = time.monotonic()
                    progress_request = urllib.request.Request(hub.rstrip("/") + prefix + "/upload-progress",
                        data=json.dumps({"bytes_done": next_offset}).encode(), method="POST",
                        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                    try:
                        # Monitoring has its own small budget and never retries
                        # inline; a quiet hub must not stall the data path.
                        relay_http(progress_request, "Hub", timeout=4)
                    except RuntimeError:
                        pass
                continue
            failures += 1
            if failures >= RELAY_ATTEMPTS:
                raise RuntimeError("OneDrive upload retries exhausted; rerun the same command to resume") from None
            time.sleep(max(retry_after, min(2 ** (failures - 1), 16)))
            if end == size and relay_complete(hub, prefix, token, probe=True):
                return
            renewed = relay_api(hub, prefix + "/session", token, {})
            if renewed.get("uploaded") is not True and relay_offset(renewed.get("next_offset", 0), size) < offset:
                restarts += 1
                if restarts > 3:
                    raise RuntimeError("OneDrive upload session repeatedly expired; resumable state was retained")
            session = renewed


def upload_onedrive(hub: str, token: str, node: str, source: Path, files: list, directories: list[str], destination: str, overwrite: bool) -> dict:
    relay_upload_url(hub)  # Refuse credentials in URLs and insecure non-loopback hubs.
    if len(files) > 10000 or sum(item[2] for item in files) > 10 * 1024 ** 3:
        raise RuntimeError("OneDrive relay tasks support at most 10,000 files and 10 GiB")
    manifest = relay_manifest(files, directories)
    context = {"hub": hub.rstrip("/"), "node": node, "source": str(source), "destination": destination,
               "overwrite": overwrite, "is_directory": source.is_dir()}
    state_path = relay_cache_path(context)
    with relay_state_lock(state_path):
        state = None
        if state_path.is_symlink():
            raise RuntimeError("Relay state must not be a symbolic link")
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if not isinstance(state, dict) or state.get("context") != context or not isinstance(state.get("idempotency_key"), str):
                    raise ValueError
            except (OSError, ValueError, UnicodeDecodeError):
                raise RuntimeError("Relay resume state is invalid; it was retained for inspection") from None
        job = None
        if state and state.get("id"):
            job = relay_api(hub, "/api/relay/transfers/" + urllib.parse.quote(str(state["id"]), safe=""), token)
        terminal = {"completed", "failed", "expired", "cancelled"}
        if state and state.get("manifest") != manifest:
            if not job or job.get("status") not in terminal:
                raise RuntimeError("Source changed; cannot resume the existing active relay task")
            state = job = None
        if job and job.get("status") in {"failed", "expired", "cancelled"}:
            state = job = None
        if state is None:
            state = {"version": 1, "context": context, "manifest": manifest, "idempotency_key": uuid.uuid4().hex}
            # Save the idempotency key before creating the server-side task.
            save_relay_state(state_path, state)
        if job is None:
            job = relay_api(hub, "/api/relay/transfers", token, {
                "node_id": node, "name": source.name, "destination": destination, "overwrite": overwrite,
                "is_directory": source.is_dir(), **manifest, "idempotency_key": state["idempotency_key"],
            })
            state["id"] = str(job["id"])
            save_relay_state(state_path, state)
        task_id = str(job["id"])
        print(f"OneDrive relay task {task_id}: {len(files)} files. File bytes bypass the hub.", flush=True)
        if job.get("status") not in {"ready", "downloading", "completed"}:
            entries = {item["path"]: item for item in job.get("files", [])}
            if set(entries) != {item[1] for item in files}:
                raise RuntimeError("Hub relay manifest does not match the local source")
            expected = {item["path"]: item for item in manifest["files"]}
            for local, relative, size in files:
                entry = entries[relative]
                if entry.get("size") != size:
                    raise RuntimeError("Hub relay file size does not match the local source")
                upload_relay_file(hub, token, task_id, entry, local, expected[relative])
                print(f"Stored in OneDrive: {relative} ({size:,} bytes)", flush=True)
            current_files, current_directories, _ = collect_files(source)
            if relay_manifest(current_files, current_directories) != manifest:
                raise RuntimeError("Source changed during upload; task was not released to the receiver")
            job = relay_api(hub, "/api/relay/transfers/" + urllib.parse.quote(task_id, safe="") + "/ready", token, {})
            if job.get("status") not in {"ready", "downloading", "completed"}:
                raise RuntimeError("Hub did not release the relay task to the receiver; resume state was retained")
        print(f"Relay status: {job.get('status', 'unknown')}. " +
              ("The hub reports that the receiver completed this task." if job.get("status") == "completed" else
               "Files are stored in OneDrive; receiver delivery is pending or in progress."), flush=True)
        print("Authenticated status endpoint: " + hub.rstrip("/") + "/api/relay/transfers/" + urllib.parse.quote(task_id, safe=""), flush=True)
        return job


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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


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
        if file_sha256(binary) == marker.read_text().strip():
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
        digest_text = file_sha256(extracted)
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


def log_tail(path: Path, limit: int = 16000) -> str:
    with path.open("rb") as source:
        source.seek(0, os.SEEK_END)
        source.seek(max(0, source.tell() - limit))
        return source.read(limit).decode(errors="replace")


def close_connection(connection: socket.socket) -> None:
    # shutdown wakes Paramiko even when it is waiting in a different thread.
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    connection.close()


def connect_direct(job: dict, key: paramiko.PKey, port: int, process: subprocess.Popen, log_path: Path, connect_timeout: int = 180) -> paramiko.SSHClient:
    pinned_key = host_key(job["ssh_host_key"])
    deadline = time.monotonic() + connect_timeout
    last_error = "direct tunnel is not ready"
    reached_ssh = False
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            # Raw FRP logs can contain credentials; main saves a redacted tail.
            raise RuntimeError(f"frpc exited before direct SSH was ready (exit code {exit_code}); see FRP diagnostics")
        client = paramiko.SSHClient()
        client.get_host_keys().add(f"[127.0.0.1]:{port}", pinned_key.get_name(), pinned_key)
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        connection = watchdog = None
        connected = False
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            connection = socket.create_connection(("127.0.0.1", port), timeout=min(5, remaining))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Paramiko's timeout also bounds SSH negotiation. FRP v0.71.1's
            # openTunnel waits up to 20s, so a 5s negotiation limit abandons live
            # XTCP attempts prematurely. Keep one socket through that window.
            # https://github.com/fatedier/frp/blob/d8cf1800dc1abd7fda1828cab105d7a6b5a50eb1/client/visitor/xtcp.go#L202
            # A socket watchdog bounds negotiation + authentication together;
            # independent phase timeouts alone can overrun the total budget.
            watchdog = threading.Timer(remaining, close_connection, args=(connection,))
            watchdog.daemon = True
            watchdog.start()
            client.connect("127.0.0.1", port=port, username=job["ssh_user"], pkey=key, sock=connection,
                           timeout=min(30, remaining), banner_timeout=min(30, remaining), auth_timeout=min(15, remaining),
                           channel_timeout=60,
                           allow_agent=False, look_for_keys=False)
            watchdog.cancel()
            if time.monotonic() >= deadline:
                raise TimeoutError("SSH negotiation/authentication exceeded the total connection budget")
            client.get_transport().set_keepalive(10)
            connected = True
            return client
        except paramiko.AuthenticationException as exc:
            if time.monotonic() < deadline:
                raise RuntimeError("Direct tunnel reached SSH, but SSH authentication failed: " + str(exc)) from exc
            last_error = "SSH authentication did not finish within the total connection budget"
        except paramiko.BadHostKeyException as exc:
            raise RuntimeError("Direct tunnel reached SSH, but the SSH host key did not match") from exc
        except (OSError, EOFError, paramiko.SSHException) as exc:
            last_error = str(exc)
        finally:
            if watchdog is not None:
                watchdog.cancel()
            if not connected:
                transport = client.get_transport()
                reached_ssh = reached_ssh or bool(getattr(transport, "remote_version", ""))
                client.close()
                if connection is not None:
                    connection.close()
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.3, remaining))
    log = log_tail(log_path)
    if reached_ssh:
        stage = "SSH handshake/authentication over the direct tunnel"
    elif "establishing nat hole connection successful" in log:
        # FRP emits that line before QUIC/KCP session.Init, not after SSH.
        stage = "P2P tunnel session establishment (UDP hole punching was reported successful; SSH not confirmed)"
    else:
        stage = "P2P UDP hole punching"
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
    parser.add_argument("--transport", choices=("p2p", "onedrive"), default="p2p", help="Explicit transport selection; no automatic fallback (default: p2p)")
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
        if args.transport == "onedrive":
            upload_onedrive(args.hub, token, args.node, source, files, directories, args.destination, args.overwrite)
            return 0
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
                    details = log_tail(log_path)
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
