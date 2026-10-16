#!/usr/bin/env bash
# Fleet upload launcher. Runtime files stay in the dedicated client directory.
set +x
set -euo pipefail
umask 077

hub='__HUB_URL__'
uv_version='0.13.1'
node=''
source_path=''
destination=''
connect_timeout=180
transport=p2p
overwrite=()

fail() { printf 'Fleet: %s\n' "$*" >&2; exit 1; }
usage() {
    printf '%s\n' '用法: --node 节点ID --source 本地路径 --destination 远端路径 [--transport p2p|onedrive] [--connect-timeout 秒数] [--overwrite]'
}
while (($#)); do
    case "$1" in
        --node|--source|--destination|--connect-timeout|--transport)
            (($# >= 2)) || fail "$1 缺少参数"
            case "$1" in
                --node) node=$2 ;;
                --source) source_path=$2 ;;
                --destination) destination=$2 ;;
                --connect-timeout) connect_timeout=$2 ;;
                --transport) transport=$2 ;;
            esac
            shift 2 ;;
        --overwrite) overwrite=(--overwrite); shift ;;
        --help|-h) usage; exit 0 ;;
        *) fail "未知参数: $1" ;;
    esac
done
[[ -n "$node" && -n "$source_path" && -n "$destination" ]] || { usage >&2; exit 2; }
[[ "$connect_timeout" =~ ^[1-9][0-9]*$ ]] || fail '--connect-timeout 必须为正整数秒数'
[[ "$transport" == p2p || "$transport" == onedrive ]] || fail '--transport 必须为 p2p 或 onedrive'
[[ -e "$source_path" ]] || fail "本地路径不存在: $source_path"
case "$hub" in https://*|http://127.0.0.1:*|http://localhost:*) ;; *) fail '启动脚本缺少有效中枢地址' ;; esac
for program in curl tar; do command -v "$program" >/dev/null || fail "系统缺少 $program"; done
if command -v sha256sum >/dev/null; then
    hash_stream() { sha256sum | awk '{print $1}'; }
elif command -v shasum >/dev/null; then
    hash_stream() { shasum -a 256 | awk '{print $1}'; }
else
    fail '系统缺少 SHA-256 校验工具（sha256sum 或 shasum）'
fi
hash_file() { hash_stream < "$1"; }

case "$(uname -s):$(uname -m)" in
    Linux:x86_64|Linux:amd64)
        target='x86_64-unknown-linux-musl'
        archive_hash='6c86040a8f045ab9adc9e3ec4cdafa03fd00e9a988ed2bf28971c7fc2d0debd0' ;;
    Linux:aarch64|Linux:arm64)
        target='aarch64-unknown-linux-musl'
        archive_hash='345a8308e4d1835cdfaadf692836fd3137a6d45864773f05ad7bf59442b1ba16' ;;
    Darwin:x86_64)
        target='x86_64-apple-darwin'
        archive_hash='bb7b805bdcf3d0bc8c74ceb94569e6b3e8aa35bd492acdbb9d46bf46d9d4ff6b' ;;
    Darwin:arm64|Darwin:aarch64)
        target='aarch64-apple-darwin'
        archive_hash='cfe454573412ff7380d15fc7dae5997ab53ffce72fce0a78e91ddbc84001acd2' ;;
    *) fail '当前支持 Linux/macOS 的 x64 和 ARM64' ;;
esac

client_root=${FLEET_CLIENT_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/fleet-hub/client}
[[ ! -L "$client_root" ]] || fail '客户端目录不能是符号链接'
mkdir -p "$client_root"
chmod 700 "$client_root"
client_root=$(cd "$client_root" && pwd -P)
export FLEET_CLIENT_HOME="$client_root"
hub_id=$(printf '%s' "$hub" | hash_stream)
config_dir="$client_root/hubs/$hub_id"
mkdir -p "$config_dir" "$client_root/bin"
chmod 700 "$config_dir" "$client_root/bin"
token_file="$config_dir/token"
token=${FLEET_TOKEN:-}
if [[ -z "$token" && -f "$token_file" ]]; then
    [[ ! -L "$token_file" ]] || fail '本地鉴权配置不能是符号链接'
    chmod 600 "$token_file"
    IFS= read -r token < "$token_file" || true
fi
if [[ -z "$token" ]]; then
    [[ -r /dev/tty ]] || fail '首次使用需要交互终端输入中枢访问令牌'
    printf '首次使用，请输入中枢访问令牌（输入不回显）: ' >/dev/tty
    IFS= read -r -s token </dev/tty || fail '未读取到中枢访问令牌'
    printf '\n' >/dev/tty
    [[ -n "$token" ]] || fail '访问令牌不能为空'
    token_tmp=$(mktemp "$config_dir/.token.XXXXXX")
    printf '%s\n' "$token" > "$token_tmp"
    chmod 600 "$token_tmp"
    mv -f "$token_tmp" "$token_file"
fi
export FLEET_TOKEN="$token"
unset token

scratch=$(mktemp -d "$client_root/.prepare.XXXXXX")
trap 'rm -rf -- "$scratch"' EXIT
uv_bin="$client_root/bin/uv-$uv_version-$target"
uv_hash="$uv_bin.sha256"
if [[ ! -x "$uv_bin" || ! -f "$uv_hash" || "$(hash_file "$uv_bin")" != "$(cat "$uv_hash")" ]]; then
    printf '%s\n' '正在自动准备上传组件，首次运行需要下载。'
    original="uv-$target.tar.gz"
    mirror="https://gh-proxy.org/https://github.com/astral-sh/uv/releases/download/$uv_version/$original"
    fallback="$hub/downloads/uv-$uv_version-$target.tar.gz"
    if ! curl -fsSL --connect-timeout 15 --max-time 300 --speed-time 30 --speed-limit 1 "$mirror" -o "$scratch/uv.tar.gz"; then
        curl -fsSL --connect-timeout 15 --max-time 300 --speed-time 30 --speed-limit 1 --retry 1 "$fallback" -o "$scratch/uv.tar.gz" || fail '上传组件下载失败，请稍后重试'
    fi
    [[ "$(hash_file "$scratch/uv.tar.gz")" == "$archive_hash" ]] || fail '上传组件校验失败，已停止执行'
    tar -xzf "$scratch/uv.tar.gz" -C "$scratch" "uv-$target/uv"
    chmod 700 "$scratch/uv-$target/uv"
    mv -f "$scratch/uv-$target/uv" "$uv_bin"
    hash_file "$uv_bin" > "$uv_hash"
    chmod 600 "$uv_hash"
fi

script_headers=()
if [[ -s "$config_dir/transfer.py" && ! -L "$config_dir/transfer.py" ]]; then
    # Hash only this small script to bind the validator to the actual cached bytes.
    # No sidecar can get out of sync when concurrent launches update the cache.
    script_headers=(-H "If-None-Match: \"$(hash_file "$config_dir/transfer.py")\"")
fi
http_status=$(curl -fsSL --connect-timeout 15 --max-time 60 --retry 1 "${script_headers[@]}" \
    -w '%{http_code}' "$hub/transfer.py" -o "$scratch/transfer.py") || fail '无法获取中枢上传组件'
case "$http_status" in
    200) [[ -s "$scratch/transfer.py" ]] || fail '中枢返回了空上传组件'
         mv -f "$scratch/transfer.py" "$config_dir/transfer.py" ;;
    304) [[ -s "$config_dir/transfer.py" && ! -L "$config_dir/transfer.py" ]] || fail '本地上传组件缓存不可用，请重试' ;;
    *) fail "中枢上传组件响应异常: $http_status" ;;
esac
chmod 600 "$config_dir/transfer.py"
export UV_CACHE_DIR="$client_root/cache"
export UV_PYTHON_INSTALL_DIR="$client_root/python"
export UV_PYTHON_BIN_DIR="$client_root/python-bin"
export UV_PYTHON_INSTALL_BIN=0
export UV_NO_MODIFY_PATH=1
export UV_PYTHON_INSTALL_MIRROR='https://gh-proxy.org/https://github.com/astral-sh/python-build-standalone/releases/download'
export PYTHONUTF8=1
printf '%s\n' '正在连接目标服务器并上传；运行环境和依赖会自动准备。'
"$uv_bin" run --no-config --no-project --managed-python --python 3.12 --script "$config_dir/transfer.py" \
    --hub "$hub" --node "$node" --source "$source_path" --destination "$destination" --connect-timeout "$connect_timeout" --transport "$transport" "${overwrite[@]}"
