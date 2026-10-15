# Fleet Hub

**一条命令接入训练服务器，通过带鉴权的远程 MCP 执行 SSH，探针面板显示节点和数据传输状态。**

日常命令经公网中枢的反向隧道执行；大文件使用 frp XTCP 从本机直连目标服务器。公开仓库的 `main` 分支为中枢发布分支，服务器每 10 分钟检查一次新提交。

## 使用

### 接入服务器

在 WebUI 输入中枢访问令牌，然后按 **接入服务器 → 填写名称 → 复制一条命令** 操作，在目标服务器终端执行一次：

```bash
curl -fsSL https://YOUR_HUB/install.sh | bash -s -- '<一次性注册令牌>'
```

程序会自动识别目标服务器环境，准备所需依赖、安装隧道与探针、完成注册并常驻运行。节点掉线会自动重连，每 10 分钟自动检查探针更新，无需手动下载客户端或管理运行环境。

注册命令单次有效、30 分钟过期，目标节点支持 Linux amd64 / arm64。以希望 Agent 使用的用户执行；接入完成后，节点会自动显示在探针面板与 MCP 清单中。程序会按运行环境建立常驻方式；systemd 环境启用开机启动。SSH 非默认端口时，执行命令前设置 `FLEET_SSH_PORT`。

### 连接 Agent

ChatGPT 网页版添加自定义 MCP 时，填写 `https://YOUR_HUB/mcp`，鉴权选择 **OAuth**，Client ID 与 Client Secret 留空，由客户端动态注册。授权页输入 Fleet 面板访问令牌后确认连接。访问令牌只用于 Fleet 验证身份，不会交给 ChatGPT；OAuth 使用 PKCE、短期访问令牌和可轮换的刷新令牌，权限为 `mcp:tools`。

已部署的旧版中枢先更新应用，再以 root 执行当前仓库的 `ops/enable_oauth.py`，将 Caddy 切换到 OAuth 验证入口。新安装会直接启用。OAuth 客户端和令牌保存在运行目录的 `oauth/`，与代码发布分离。

Codex 等支持自定义请求头的客户端也可继续使用：

| 配置 | 内容 |
| --- | --- |
| 传输 | Streamable HTTP |
| URL | `https://YOUR_HUB/mcp` |
| 鉴权头 | `Authorization: Bearer <中枢访问令牌>` |

常用工具：

- `fleet_status`、`fleet_enroll`、`fleet_revoke`：节点列表、注册、撤销。
- `ssh_hosts`、`ssh_check`、`ssh_exec`、`ssh_fanout`：清单、连通检查、单台及批量命令。
- `ssh_spawn`、`session_send`、`session_recv`、`session_list`、`session_kill`：交互式 SSH。
- `ssh_upload`、`ssh_download`：中枢与目标之间的 SFTP。

`host` 使用节点 ID。MCP 命令策略为 `open`，未开启逐条命令审批。SFTP 的 `local_path` 指中枢服务器上的路径；电脑上的数据集使用本地传输客户端。

### 上传数据集

在面板下方的 **一键上传命令** 中，选择目标服务器，填入本机文件或目录路径、服务器保存路径，复制命令并在本机终端执行。面板自动选择当前系统对应的命令，也可切换终端类型。

Linux / macOS：

```bash
curl -fsSL https://YOUR_HUB/send.sh | bash -s -- --node node-xxxxxxxxxx --source ./dataset --destination /data/dataset
```

Windows PowerShell：

```powershell
& ([scriptblock]::Create((Invoke-WebRequest -UseBasicParsing 'https://YOUR_HUB/send.ps1').Content)) -Node node-xxxxxxxxxx -Source 'D:\datasets\dataset' -Destination /data/dataset
```

命令会自动准备上传工具。首次执行时按提示输入中枢访问令牌，程序将授权保存到本机，之后直接使用。无需手动设置环境变量或下载安装运行时。

上传通过独立临时 SSH 密钥建立 XTCP 直连，文件内容不通过中枢转发。面板显示字节数、进度、平均速度和失败原因。

直连打洞重试窗口约 60 秒，SSH 握手及认证各最多等待 15 秒；失败即停止，不回退到中枢中继。当前支持文件与目录上传，不支持断点续传或符号链接。覆盖同名文件可勾选面板中的替换选项，或在命令后添加 `--overwrite`（Linux / macOS）或 `-Overwrite`（PowerShell）。

### 节点在线状态

探针每 10 秒上报；最近心跳不足 35 秒且 frp SSH 代理为 `online` 时，节点显示在线。WebUI 每 5 秒刷新。传输任务超过 90 秒未更新进度，会标记为失败。

### 探针面板

浅色面板提供 CPU、内存、根磁盘和 GPU 四个环形仪表，点击可展开 1h / 6h / 24h 的真实历史曲线。内存和磁盘显示已用/总容量；多 GPU 可切换整体平均核心占用或单卡。无 GPU 显示 `—`，驱动不可用、权限不足和采集失败分别显示状态，探针不安装显卡驱动。

**延迟是节点到中枢的 HTTPS 心跳请求 RTT**，使用单调时钟计时，在下一次心跳上传结果。它与 ICMP ping、SSH 隧道 RTT 是不同指标。每卡的 96 根条对应 UTC 对齐的 15 分钟桶，含当前正在增长的桶；Tooltip 按浏览器时区显示时间。均值和 P95 由真实成功样本计算，失败计入样本数，明确离线、未知时段分别显示深灰和浅灰。延迟摘要每 45 秒更新，已展开历史每 60 秒更新。

默认资源提醒/严重阈值为 75% / 90%，延迟 P95 正常/偏高上限为 100ms / 250ms；在“设置”中可调整全局或某个节点的阈值，保存到当前浏览器，节点恢复默认后重新继承全局值。告警中心汇总当前观测，未配置站外通知。页面失去连接时保留上次数据并标明过期。

在节点菜单或“节点管理”中编辑国家代码、地区和城市。人工设置优先于节点上报，未知地区不进行猜测；未提供公网 IP 时显示未知。地区不参与身份验证。

历史保存到现有 SQLite 数据库，按分钟聚合资源，保留 RTT 原始样本以计算精确 P95。记录至少保留 48 小时，每小时清理。服务重启保留历史；没有观测的时间段返回 `null`，不插值。节点撤销后立即停止通过 API 展示其历史，记录继续按保留期清理。

所有管理 API 使用原管理员 Bearer；节点凭据只能上传自身心跳：

| 接口 | 用途 |
| --- | --- |
| `GET /api/state` | 最新指标、GPU、容量、地区、RTT 和 `freshness` |
| `GET /api/nodes/{id}/history?metric=cpu&range=1h` | `cpu/memory/disk/gpu`、`1h/6h/24h`，GPU 可附 `device=0` |
| `GET /api/latency/summary?range=24h&bucket=15m` | 所有活动节点各 96 桶，样本数、成功数、均值/P95、原因 |
| `PUT /api/nodes/{id}/location` | 完整替换 `country_code/country_name/region/city`，空字符串清空 |

老探针的心跳负载继续兼容，缺少的新指标保持未知；节点自动更新后开始上报新字段。

## 仓库与自动发布

公开仓库为 [witrer/fleet-hub](https://github.com/witrer/fleet-hub)，默认发布分支 `main`。中枢通过 GitHub 加速镜像 `https://gh-proxy.org/https://github.com/witrer/fleet-hub.git` 拉取，无需 Token 或 Deploy Key。镜像 URL 格式见 [GH-Proxy 官方使用指南](https://gh-proxy.com/docs/github-accelerator/)。

后续在开发机向官方 GitHub 远端提交改动，中枢再通过镜像获取。以下开发机命令中的 `origin` 指官方仓库：

```bash
git add app wheels
git commit -m "Describe the change"
git push origin main
```

中枢每 10 分钟检查 `main`，有新提交时发布 `app/` 和 `wheels/`。常驻节点每 10 分钟向中枢检查探针新版本；上传命令会自动使用中枢提供的最新工具。操作系统、frp 和 Caddy 由单独的运维操作管理。

WebUI 的 **中枢更新** 区域显示当前提交短 SHA、最近检查时间、部署结果及失败说明。更新器记录在 `/var/lib/fleet/update-status.json`，仅经过管理员鉴权的 `/api/state` 会返回这些信息。状态文件缺失或暂时不可读时，不影响节点、SSH 和探针接口。

### 一次性接管已有中枢

接管脚本用于已经运行 Fleet 的服务器，要求保留现有 `/opt/fleet/app`、`/opt/fleet/venv` 和 `/var/lib/fleet`。

通过加速镜像将仓库检出到 `/opt/fleet/repo`：

```bash
git clone --branch main --single-branch \
  https://gh-proxy.org/https://github.com/witrer/fleet-hub.git /opt/fleet/repo
```

将 `ops/updater-config.example.json` 保存为 `/etc/fleet-updater.json`，保留示例中的路径及 `branch: main`。更新器直接使用仓库的 `origin` 拉取，无需配置拉取凭据。

已存在代码仓库时，使用以下命令将拉取地址切换到同一镜像：

```bash
git -C /opt/fleet/repo remote set-url origin \
  https://gh-proxy.org/https://github.com/witrer/fleet-hub.git
```

然后以 root 执行：

```bash
/opt/fleet/venv/bin/python /opt/fleet/repo/ops/install_updater.py
```

脚本会保存现有应用为初始回滚版本，将下载包移入共享目录，安装更新器及 systemd 配置，并检查原有中枢和 MCP 服务。首次 Git 更新成功后，才启用 10 分钟定时器。运行数据库、令牌、节点密钥和证书状态继续沿用。

### 检查与控制更新

以下命令在中枢 root SSH 终端执行：

```bash
# 查看每 10 分钟运行的更新定时器
systemctl status fleet-update.timer --no-pager
systemctl list-timers fleet-update.timer --no-pager

# 立即检查一次
systemctl start fleet-update.service

# 查看检查、部署与回滚结果
journalctl -u fleet-update -n 50 --no-pager

# 修复环境后，显式重试上次失败的提交
/opt/fleet/venv/bin/python /opt/fleet/ops/fleet_update.py --retry

# 暂停后续自动检查
systemctl stop fleet-update.timer

# 恢复并启用开机检查
systemctl enable --now fleet-update.timer
```

已有服务的接管沿用 `/var/lib/fleet` 内的数据库、令牌、节点密钥和证书状态。代码仓库位于 `/opt/fleet/repo`，更新器独立配置位于 `/etc/fleet-updater.json`。镜像入口由该仓库的 `origin` 配置，修改后会在下一次更新检查时使用；不改动系统全局 Git 配置。运行数据不进入 Git。

## 布局

| 路径 | 用途 |
| --- | --- |
| `app/app.py` | 注册、探针、传输状态和 frp 鉴权 API |
| `app/probe.py` | 分钟历史、延迟聚合和地区配置 |
| `app/fleet_oauth.py` | OAuth 发现、注册、所有者授权及 MCP 鉴权 |
| `app/mcp_entry.py` | Relay Shell 与 Fleet 节点工具 |
| `app/static/index.html` | 无构建依赖的中文 WebUI |
| `app/static/node.py` | Linux 节点安装及探针 |
| `app/static/send.sh`、`app/static/send.ps1` | 一条命令上传的系统适配与启动入口 |
| `app/static/transfer.py` | 本地 XTCP 传输客户端 |
| `app/assets.json` | frp 下载文件名与 SHA-256 |
| `deploy_fleet.py` | 初始服务和系统配置生成 |
| `ops/install_updater.py` | 接管现有中枢并启用自动发布 |
| `ops/fleet_update.py` | 拉取、健康检查、发布与失败回滚 |
| `patches/`、`wheels/` | Relay Shell 清单热更新补丁与构建产物 |
| `licenses/`、`SOURCES.md` | 上游许可证、版本与来源 |

frp 下载包不提交 Git；生产环境保留 `/opt/fleet/app/downloads`。仓库中的空目录用于开发和测试。

## 本地验证

Python 3.12 或更新的兼容环境中，安装项目 wheel、FastAPI、HTTPX 和 pytest 后运行：

```bash
python -m pytest app/test_app.py app/test_oauth.py app/test_probe.py app/test_probe_api.py app/test_node.py app/test_node_metrics.py -q
```

中枢健康检查为 `/health`；实际节点状态、SSH 可用性和更新结果通过鉴权 MCP 或 WebUI 查看。

## HTTPS 与来源

当前部署使用可信 IP HTTPS 证书。Caddy 使用 HTTP-01 与 `shortlived` 配置自动管理证书；TCP 80 用于验证和 HTTPS 跳转，TCP 443 提供 WebUI 与 MCP。短期证书续期需要保留验证入口和 Caddy 状态目录。

上游：frp `0.71.1`、Relay Shell `0.4.0`（`a4e4835fb81f22d18208c468423ddf0d954ceb62`）、Caddy `2.11.7`。各自许可证与修改来源见 [SOURCES.md](SOURCES.md) 和 [licenses/](licenses/)。
