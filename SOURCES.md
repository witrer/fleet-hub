# Fleet 源码与构建来源

本包对应 `https://1.95.165.162` 的 Fleet 中枢实现。部署配置由 `deploy_fleet.py` 生成；初次运行会在服务器上生成新的令牌和密钥。

## 目录

| 路径 | 内容 |
| --- | --- |
| `app/` | FastAPI 中枢、MCP 入口、探针、WebUI、传输客户端及应用测试 |
| `app/assets.json` | frp 发行文件名与 SHA-256 校验值 |
| `deploy_fleet.py` | 中枢服务、frps、Caddy、systemd 配置生成脚本 |
| `patches/relay-inventory-hot-reload.patch` | Relay Shell 服务器清单热更新补丁及相关测试 |
| `wheels/relay_shell-0.4.0-py3-none-any.whl` | 包含热更新补丁的 Relay Shell 构建产物 |
| `licenses/` | 上游许可证与 Relay Shell NOTICE |
| `SHA256SUMS` | 本包各文件的 SHA-256 校验清单 |

## 上游项目

| 项目 | 版本 / 提交 | 官方来源 | 许可 |
| --- | --- | --- | --- |
| frp | `0.71.1` | <https://github.com/fatedier/frp/releases/tag/v0.71.1> | Apache-2.0 |
| Relay Shell | `0.4.0`，提交 `a4e4835fb81f22d18208c468423ddf0d954ceb62` | <https://github.com/rmednitzer/relay-shell/tree/a4e4835fb81f22d18208c468423ddf0d954ceb62> | Apache-2.0 |
| Caddy | `2.11.7` | <https://github.com/caddyserver/caddy/releases/tag/v2.11.7> | Apache-2.0 |

Relay Shell wheel 从上述提交应用附带补丁后构建。重新构建：

```bash
git clone https://github.com/rmednitzer/relay-shell.git
cd relay-shell
git checkout a4e4835fb81f22d18208c468423ddf0d954ceb62
git apply ../patches/relay-inventory-hot-reload.patch
uv build --wheel
```

frp 与 Caddy 使用对应版本官方发行二进制。为缩小源码交付体积，本包未包含其二进制及 `app/downloads`；部署时按 `app/assets.json` 中的文件名下载 frp 发行包，验证对应 SHA-256，并放入 `/opt/fleet/app/downloads`。

## 本次实现

- `app/app.py`：节点注册、一次性接入命令、独立节点鉴权、探针指标、frp 状态合并、传输任务与鉴权钩子。
- `app/mcp_entry.py`：在 Relay Shell 上追加 `fleet_status`、`fleet_enroll`、`fleet_revoke`。
- `app/static/node.py`：Linux 节点安装、SSH 公钥接入、frpc 进程管理与探针心跳。
- `app/static/transfer.py`：本机经 XTCP 直连上传并上报进度，使用脚本内 PEP 723 依赖声明。
- `app/static/index.html`：无构建依赖的中文探针面板。
- Relay Shell 补丁：清单变更后热更新，新注册服务器无需重启 MCP 服务即可使用。

## 部署脚本的前置环境

`deploy_fleet.py` 在 Linux 中以 root 运行，目标应用目录固定为 `/opt/fleet/app`，运行状态目录为 `/var/lib/fleet`。它假定以下文件已经准备好：

- `/opt/fleet/app`：本包 `app/` 内容，及上述 `downloads/` 发行文件。
- `/opt/fleet/bin/frps`：frp 0.71.1 Linux amd64 服务端。
- `/opt/fleet/bin/caddy`：Caddy 2.11.7 Linux amd64。
- `/opt/fleet/venv`：Python 3.12 或更新的兼容版本环境，安装本包 Relay Shell wheel，以及 FastAPI、HTTPX、Uvicorn、AsyncSSH。
- 系统命令：OpenSSL、systemd 和常规 Linux 用户管理工具。

当前 Caddy 使用 IP HTTPS、HTTP-01、`shortlived` 证书配置、`default_sni 1.95.165.162` 及 systemd 环境 `GODEBUG=tlsmlkem=0`。MCP 入口由 `mcp_entry.py` 启动，命令执行模式为 `open`。公网 HTTP-01 验证需要 TCP 80 可达。

本包包含源码与可复现来源，不含服务器 root 密码、`fleet-access.json`、生产运行配置、SSH 私钥或证书私钥。
