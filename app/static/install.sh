#!/usr/bin/env bash
set -euo pipefail
HUB='__HUB_URL__'
TOKEN="${1:?需要一次性注册令牌}"
command -v python3 >/dev/null || { echo '需要 Python 3'; exit 1; }
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
curl -fsSL "$HUB/node.py" -o "$TMP"
python3 "$TMP" install --hub "$HUB" --token "$TOKEN"
