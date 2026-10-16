#!/usr/bin/env bash
set -euo pipefail
umask 077
HUB="${FLEET_HUB:-__HUB_URL__}"
ROOT="${FLEET_NODE_DIR:-$HOME/.local/share/fleet-node}"
TOKEN="${1:-}"
export FLEET_NODE_DIR="$ROOT"
mkdir -p "$ROOT/runtime"
usable_python() { "$1" -c 'import sys,ssl,urllib.request; assert sys.version_info >= (3,8)' >/dev/null 2>&1; }
# Keep a previous user-space installation usable even when it is not on PATH.
# Explicit FLEET_PYTHON and system interpreters retain their existing priority.
PYTHON=''
for candidate in "${FLEET_PYTHON:-python3}" python3 python "$ROOT"/runtime/python/*/bin/python3; do
  if command -v "$candidate" >/dev/null 2>&1 && usable_python "$candidate"; then
    PYTHON=$(command -v "$candidate"); break
  fi
done
# A slow but progressing download may continue; a dead mirror cannot stall a
# deployment indefinitely before the existing hub fallback gets a turn.
download() {
  curl -fSL --connect-timeout 10 --speed-limit 1 --speed-time 30 \
    --retry 1 --retry-delay 1 --retry-max-time 60 "$1" -o "$2"
}
TMP=''
UV_ARCHIVE=''
trap 'rm -f -- "${TMP:-}" "${UV_ARCHIVE:-}"' EXIT
if [ -z "$PYTHON" ] && [ "$(id -u)" = 0 ]; then
  echo '正在自动安装运行环境…'
  if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq python3 ca-certificates || true
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 ca-certificates || true
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3 ca-certificates || true
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache python3 ca-certificates || true
  fi
  if command -v python3 >/dev/null 2>&1 && usable_python python3; then PYTHON=$(command -v python3); fi
fi
if [ -z "$PYTHON" ]; then
  case "$(uname -m)" in
    x86_64) UV_ARCH=x86_64; UV_SHA=6c86040a8f045ab9adc9e3ec4cdafa03fd00e9a988ed2bf28971c7fc2d0debd0 ;;
    aarch64|arm64) UV_ARCH=aarch64; UV_SHA=345a8308e4d1835cdfaadf692836fd3137a6d45864773f05ad7bf59442b1ba16 ;;
    *) echo '支持 Linux amd64/arm64'; exit 1 ;;
  esac
  echo '正在自动准备用户运行环境…'
  if [ ! -x "$ROOT/runtime/uv" ]; then
    UV_ARCHIVE=$(mktemp "$ROOT/runtime/uv.XXXXXX")
    download "https://gh-proxy.org/https://github.com/astral-sh/uv/releases/download/0.13.1/uv-${UV_ARCH}-unknown-linux-musl.tar.gz" "$UV_ARCHIVE" || \
      download "$HUB/downloads/uv-0.13.1-${UV_ARCH}-unknown-linux-musl.tar.gz" "$UV_ARCHIVE"
    echo "$UV_SHA  $UV_ARCHIVE" | sha256sum -c -
    tar -xzf "$UV_ARCHIVE" -C "$ROOT/runtime" --strip-components=1 "uv-${UV_ARCH}-unknown-linux-musl/uv"
    rm -f "$UV_ARCHIVE"
  fi
  export UV_PYTHON_INSTALL_DIR="$ROOT/runtime/python"
  export UV_CACHE_DIR="$ROOT/runtime/cache"
  "$ROOT/runtime/uv" python install 3.12 --no-bin --mirror 'https://gh-proxy.org/https://github.com/astral-sh/python-build-standalone/releases/download'
  PYTHON=$("$ROOT/runtime/uv" python find --managed-python 3.12)
fi
TMP=$(mktemp "$ROOT/node-install.XXXXXX.py")
for attempt in 1 2; do
  download "$HUB/node.py" "$TMP"
  if "$PYTHON" - "$HUB" "$TMP" <<'PY'
import hashlib,json,pathlib,sys,urllib.request
with urllib.request.urlopen(sys.argv[1]+'/api/agent-release',timeout=30) as response:
    release=json.load(response)
source=pathlib.Path(sys.argv[2]).read_bytes()
if hashlib.sha256(source).hexdigest()!=release['sha256']:
    raise SystemExit(75)
compile(source, 'node.py', 'exec')
PY
  then
    break
  else
    verify_status=$?
    # Only a checksum mismatch can be a publication race. Network, manifest
    # and compile failures keep their original diagnostics and are not retried.
    if [ "$verify_status" != 75 ]; then exit "$verify_status"; fi
    if [ "$attempt" = 2 ]; then
      echo '节点程序下载校验失败，请稍后重新执行安装命令' >&2
      exit "$verify_status"
    fi
    echo '节点程序校验不一致，正在重新下载并校验…' >&2
  fi
done
"$PYTHON" "$TMP" install --hub "$HUB" --token="$TOKEN"
