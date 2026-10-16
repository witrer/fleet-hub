# Fleet Hub

**一条命令接入训练服务器，通过带鉴权的远程 MCP 执行 SSH，探针面板显示节点和数据传输状态。**

日常命令经公网中枢的反向隧道执行；大文件默认使用 frp XTCP 直连，也可由管理员配置个人 OneDrive 中转，两种文件通道都不占用中枢转发带宽。公开仓库的 `main` 分支为中枢发布分支，服务器每 10 分钟检查一次新提交。

## 使用

### 接入服务器

在 WebUI 输入中枢访问令牌，然后按 **接入服务器 → 填写名称 → 复制一条命令** 操作，在目标服务器终端执行一次：

```bash
curl -fsSL https://YOUR_HUB/install.sh | bash -s -- '<一次性注册令牌>'
```

程序会自动识别目标服务器环境，准备所需依赖、安装隧道与探针、完成注册并常驻运行。节点掉线会自动重连，每 10 分钟自动检查探针更新，无需手动下载客户端或管理运行环境。

注册命令单次有效、30 分钟过期，目标节点支持 Linux amd64 / arm64。以希望 Agent 使用的用户执行；接入完成后，节点会自动显示在探针面板与 MCP 清单中。程序会按运行环境建立常驻方式；systemd 环境启用开机启动。SSH 非默认端口时，执行命令前设置 `FLEET_SSH_PORT`。

安装优先使用现有 Python，也会识别此前安装在节点目录中的运行时；镜像连续 30 秒没有下载进展后转入重试或中枢备用地址。安装脚本与发布清单版本交错导致 SHA 不符时，最多重新下载校验一次，验证通过后才执行。FRP 安装使用临时文件和流式解压，下载时完成一次 SHA-256 校验，再原子替换程序；中断或校验失败保留旧程序。

节点自动更新保留固定入口 `node.py` 和保护器 `node-supervisor.py`，只替换 `node-agent.py`。安装候选前保存上一版本；默认观察 60 秒，本地初始化须在 90 秒内完成且同一个 FRP 子进程存活至少 5 秒。导入失败、语法错误、早退或启动未就绪会恢复旧版本，并记住最近 8 个失败 SHA，避免反复安装同一个坏版本。确认仅证明本地初始化与 FRP 进程存活，不证明公网隧道或 MCP 已可用。

旧节点取得此版本和保护器后，会沿用原启动命令自动迁移，保留节点身份；迁移成功后的升级才具备上述保护。保护器自身、Python 运行时、FRP 二进制和既有 FRP 配置暂不自动升级。更新状态和当前版本可在节点管理页查看。

### 连接 Agent

ChatGPT 网页版添加自定义 MCP 时，填写 `https://YOUR_HUB/mcp`，鉴权选择 **OAuth**，Client ID 与 Client Secret 留空，由客户端动态注册。授权页输入 Fleet 面板访问令牌后确认连接。访问令牌只用于 Fleet 验证身份，不会交给 ChatGPT；OAuth 使用 PKCE、短期访问令牌和可轮换的刷新令牌，权限为 `mcp:tools`。

已部署的旧版中枢先更新应用，再以 root 执行当前仓库的 `ops/enable_oauth.py`，将 Caddy 切换到 OAuth 验证入口。新安装会直接启用。OAuth 客户端和令牌保存在运行目录的 `oauth/`，与代码发布分离。

需要为 OAuth 使用独立域名时，在运行目录 `config.json` 设置 `oauth_base_url`（例如 `https://fleet.example.com`）。面板返回的 MCP URL、OAuth 发现、授权页与令牌资源使用该地址；节点安装、下载和更新继续使用原 `base_url`，已接入节点无需重新注册。保存配置时保留原服务账号的文件所有权和 `0600` 权限，然后重启 `fleet-hub`。切换 OAuth 地址后，在客户端重新连接以获取面向新资源的令牌；使用新连接页面生成的准确回调地址。

使用 Cloudflare Named Tunnel 时，将公开域名路由到现有 Caddy：服务填 `https://127.0.0.1:443`，附加设置中的 **Origin Server Name** 和 **HTTP Host Header** 均填写现有 Caddy 证书对应的中枢 IP 或域名，保留 TLS 证书验证。`cloudflared` 通过受限权限令牌文件运行并由 systemd 常驻；无需新增入站端口。先验证公开域名回源成功，再切换 `oauth_base_url`。若直接将域名解析到中枢，则需要在 Caddy 配置对应域名与可信证书；重跑初始部署脚本前应保留自定义 Caddy 配置。

Codex 等支持自定义请求头的客户端也可继续使用：

| 配置 | 内容 |
| --- | --- |
| 传输 | Streamable HTTP |
| URL | `https://YOUR_HUB/mcp` |
| 鉴权头 | `Authorization: Bearer <中枢访问令牌>` |

常用工具：

- `fleet_status`、`fleet_enroll`、`fleet_revoke`：节点列表、注册、撤销。
- `fleet_monitor`：直接读取已存的 CPU、内存、磁盘或 GPU 历史，无需先建立 SSH。
- `ssh_hosts`、`ssh_check`、`ssh_exec`、`ssh_fanout`：清单、连通检查、单台及批量命令。
- `ssh_spawn`、`session_send`、`session_recv`、`session_list`、`session_kill`：交互式 SSH。
- `ssh_upload`、`ssh_download`：中枢与目标之间的 SFTP。

`host` 使用节点 ID。MCP 命令策略为 `open`，未开启逐条命令审批。SFTP 的 `local_path` 指中枢服务器上的路径；电脑上的数据集使用本地传输客户端。

模型只需查看某台节点时，可调用 `fleet_status(node_id="node-xxxxxxxxxx", detail=false)`，减少无关字段与其他节点的传输记录；不传参数仍保留原有完整返回。`fleet_monitor(node_id="node-xxxxxxxxxx", metric="cpu", range="1h")` 默认只返回窗口统计、最新分钟与最后有观测的分钟（均带时间），需要完整曲线时再传 `include_points=true`。缺失值保持 `null`，不会用旧值冒充当前观测。两者继续经过原审计、输出上限和权限策略；Relay 的 `readonly` 模式仍按未知工具的保守 Tier 1 分类拒绝 Fleet 自定义工具。

Fleet 工具在 MCP 服务生命周期内复用回环 HTTP 连接池，最多 8 条连接、4 条保活连接；连接和等待连接池各限 2 秒，总请求限 20 秒。超时不会自动重试注册或撤销操作；调用方应先查询状态，避免重复变更。SSH 仍复用 Relay Shell 自带连接池。

短期后台交互可用 `ssh_spawn` 返回会话 ID，之后用 `session_recv(timeout=0)` 立即读取已有输出。会话只保存在 MCP 进程内存中，MCP 重启或闲置清理会关闭，不适合作为持久训练任务。MCP 通知不保证主动唤醒 Codex；定时作业、监控事件和宿主调度的后续设计见 [后台任务评估](docs/mcp-background-tasks.md)。

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

已缓存的上传脚本通过内容 ETag 检查更新；没有变化时返回 304，不重复下载。下载失败保留缓存文件并停止本次启动，不执行残缺的新脚本。

默认 P2P 上传通过独立临时 SSH 密钥建立 XTCP 直连，文件内容不通过中枢转发。面板显示字节数、进度、平均速度和失败原因。

直连打洞与 SSH 建连默认最多重试 180 秒，可用 `--connect-timeout 300`（Linux / macOS）或 `-ConnectTimeout 300`（PowerShell）延长；失败会区分 P2P 打洞、SSH 握手与认证阶段，不回退到中枢中继。当前支持文件与目录上传，不支持断点续传或符号链接。覆盖同名文件可勾选面板中的替换选项，或在命令后添加 `--overwrite`（Linux / macOS）或 `-Overwrite`（PowerShell）。

回环端口连接最多等待 5 秒，SSH 握手允许最多 30 秒以覆盖 FRP 的隧道等待窗口，各阶段仍受整体连接截止时间约束。认证失败或主机密钥不匹配立即结束。FRP 报告打洞成功只表示中间步骤完成，最终以 SSH 握手和认证成功为准；UDP 被封锁或 NAT 不兼容仍可能导致直连失败。

传输面板区分工具准备、P2P / SSH 连接和文件上传，分别显示准备时间与传输时间。新客户端的“活跃均速”从文件传输阶段开始计时，不包含工具下载或打洞等待；旧客户端仍显示原有平均速度。连接阶段失败且已传输 0 字节，不能据此判断上传带宽。

Cloudflare R2 预签名分片备用通道目前处于设计阶段，尚未接入；当前失败不会自动转入 R2。官方限额、无头下载与恢复接口见 [Cloudflare 评估](docs/cloudflare-fallback.md)，本轮实现和验证范围见 [优化报告](docs/optimization-review.md)。

### OneDrive 临时中转

管理员准备自己的 Microsoft 应用注册后，可在 WebUI 的 OneDrive 设置中填写 client ID、点击登录，按微软页面提示完成设备码授权。中枢保存配置并立即启用，无需再手动复制 token 或重启服务；完整前置条件见 [个人 OneDrive 配置说明](docs/onedrive.md)。随后面板可选择 **OneDrive 中转**；现有命令也可添加 `--transport onedrive`，PowerShell 对应 `-Transport onedrive`。客户和训练机无需分别登录网盘。默认仍为 P2P，本版采用显式通道选择，没有自动切换策略。

本地通过 10MiB 顺序分片直接上传 OneDrive；训练机通过心跳领取任务、后台直接下载。中枢的鉴权下载入口返回 302 到当前 OneDrive 文件地址，不代理文件正文。上传到网盘后客户端即可退出，面板分别显示上传与接收进度，训练机确认落盘后才显示完成。旧节点须先自动更新并上报接收能力，才能创建 OneDrive 任务，避免待收任务反过来阻塞节点升级。

任务默认有效 24 小时、总量最多 10GiB，创建前检查真实网盘剩余空间。上传进程中断可重新运行原命令恢复；会话过期会重新上传该文件。下载支持 Range、短链刷新、落盘前版本复核及原子提交。完成、失败、取消或到期后，使用官方 `permanentDelete` 永久清理任务绑定的 Fleet 暂存文件，避免新增回收站占用；不会清空其他网盘文件。清理失败保留并重试，WebUI 显示待清理数和错误。历史上已进入回收站的文件仍需管理员人工处理；配额刷新可能延迟，真实账户行为与 10GB 公网吞吐仍需验证。

### 节点在线状态

探针每 10 秒上报；最近心跳不足 35 秒且 frp SSH 代理为 `online` 时，节点显示在线。WebUI 每 5 秒刷新。传输任务超过 90 秒未更新进度，会显示“进度暂时失联”，保留最后一次字节数，等待客户端确认结果；控制面失联不会直接撤销仍有效的传输授权。

探针仍有新鲜心跳，但中枢未取得 SSH 隧道状态时，面板显示“隧道状态未知”，不计入已确认的离线节点。探针更新检查与下载在单个后台任务中运行，避免更新接口缓慢拖延心跳；真正应用新版本时仍会重启隧道并自动重连。

中枢在传输授权准备、或有效任务仍处于连接/上传阶段时，通过心跳要求节点延后更新。候选保留在内存中，等待空闲后安装，不重复下载；心跳失败也暂缓安装。这是当前任务快照的保护，尚非更新与新任务之间的原子互斥，不能保证消除所有启动竞态。进度失联的任务在结束或授权到期前仍阻止更新。

基础资源采集也使用单个后台线程；慢磁盘读取不会阻塞首个心跳和隧道进程监督。首次 CPU 尚无两次差分时显示未知。新探针上报 `metrics_status` 和采样开始时间 `metrics_sampled_at`，采集中、失败或超过 35 秒的样本不会伪装成新鲜数值。`freshness` 表示资源采样新鲜度，`heartbeat_freshness` 单独表示心跳新鲜度；旧探针未上报这些字段时保留原心跳口径。相同基础采样不会重复计入资源历史，GPU 与 RTT 独立记录；分钟资源曲线仍按中枢收到样本的分钟聚合。

中枢每 5 秒巡检 FRP，单次请求耗时计入该周期。某轮探针或数据库记录失败后会继续下一轮巡检；FRP 快照超过 20 秒未更新即失效，不能继续确认节点在线。节点管理页显示中枢启动耗时、运行时间、巡检与 FRP 状态、最近巡检时间；详情含请求耗时、最近成功时间和调度延迟。这些诊断仅通过管理员鉴权的 `/api/state` 返回；`/health` 仍仅表示 HTTP 服务存活。

中枢 RSS 指 HTTP 服务进程的驻留内存，不包含 MCP、FRP、Caddy 或整台主机。部署准备耗时与依赖复用状态单独展示，避免把它们误当作整体发布耗时或整机内存。

已完成或明确失败的任务保持终态，迟到进度不会覆盖结果；活动任务的字节数与计时也不会倒退。传输授权仍在 24 小时后过期，节点撤销立即阻止新的隧道连接。授权到期但尚未收到结果时，面板保留“文件结果待确认”，不把它当作已确认的文件失败。

### 探针面板

浅色面板提供 CPU、内存、根磁盘和 GPU 四个环形仪表，点击可展开 1h / 6h / 24h 的真实历史曲线。内存和磁盘显示已用/总容量；多 GPU 可切换整体平均核心占用或单卡。无 GPU 显示 `—`，驱动不可用、权限不足和采集失败分别显示状态，探针不安装显卡驱动。

节点和中枢还显示上下行速率（B/s）、接口累计收发字节、错误/丢弃计数和采样接口；节点可展开 1h / 6h / 24h 速率历史，MCP `fleet_monitor` 同样支持 `network_rx`、`network_tx`。采样优先选择最低路由度量的默认路由接口，避免同时累计 VPN 与底层网卡；没有默认路由时回退到活跃物理网卡或非虚拟接口。累计值来自接口生命周期，机器重启、接口重建或选择改变可能归零/变化，并非云厂商账单或 Fleet 文件字节。速率使用单调时钟差分；首次采样、计数回退、接口切换和超过 35 秒的旧样本显示未知，历史缺失保留空白。错误/丢弃计数不是端到端丢包率。

**延迟是节点到中枢的 HTTPS 心跳请求 RTT**，使用单调时钟计时，在下一次心跳上传结果。它与 ICMP ping、SSH 隧道 RTT 是不同指标。每卡的 96 根条对应 UTC 对齐的 15 分钟桶，含当前正在增长的桶；Tooltip 按浏览器时区显示时间。均值和 P95 由真实成功样本计算，失败计入样本数，明确离线、未知时段分别显示深灰和浅灰。延迟摘要每 45 秒更新，已展开历史每 60 秒更新。

默认资源提醒/严重阈值为 75% / 90%，延迟 P95 正常/偏高上限为 100ms / 250ms；在“设置”中可调整全局或某个节点的阈值，保存到当前浏览器，节点恢复默认后重新继承全局值。告警中心汇总当前观测，未配置站外通知。页面失去连接时保留上次数据并标明过期。

面板仅渲染当前页面，历史和延迟摘要按需查询；相同摘要的 96 根延迟条会复用。摘要查询失败后也间隔至少 45 秒自动重试，手动刷新可立即重试。后台标签页暂停定时查询，切回后刷新。

在节点菜单或“节点管理”中编辑国家代码、地区和城市。人工设置优先于节点上报，未知地区不进行猜测；未提供公网 IP 时显示未知。地区不参与身份验证。

历史保存到现有 SQLite 数据库，按分钟聚合资源，保留 RTT 原始样本以计算精确 P95。记录至少保留 48 小时，每小时清理。服务重启保留历史；没有观测的时间段返回 `null`，不插值。节点撤销后立即停止通过 API 展示其历史，记录继续按保留期清理。

24 小时延迟摘要最多缓存 15 秒，保留真实生成时间，减少多个面板重复扫描历史与计算 P95。不同阈值、节点清单和新的 15 分钟时间桶分别生成摘要；节点撤销立即从后续查询结果移除。

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

更新器比较 `wheels/` 的 Git 树与 Python 运行环境；均未变化时复制当前发布的独立依赖目录，跳过重复解包和安装 wheel。仍执行应用编译、依赖导入、服务健康检查和失败回滚；不会用硬链接共享可变依赖。校验后的发布目录移除 wheel 副本，源文件仍可从 Git 恢复。此优化位于 `ops/fleet_update.py`，现有自动发布仅覆盖 `app/` 和 `wheels/`，需单独更新已安装的运维脚本后才生效。

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
| `app/onedrive.py`、`app/relay_transfer.py` | OneDrive Graph 元数据、持久中转任务、下载跳转及永久清理 |
| `app/onedrive_auth.py` | WebUI 设备码登录、配置持久化、账号切换保护及崩溃恢复 |
| `app/static/index.html` | 无构建依赖的中文 WebUI |
| `app/static/node.py` | Linux 节点安装及探针 |
| `app/static/node-supervisor.py` | 固定节点启动保护、候选确认和失败回滚 |
| `app/static/send.sh`、`app/static/send.ps1` | 一条命令上传的系统适配与启动入口 |
| `app/static/transfer.py` | 本地 XTCP 传输客户端 |
| `app/assets.json` | frp 下载文件名与 SHA-256 |
| `deploy_fleet.py` | 初始服务和系统配置生成 |
| `ops/install_updater.py` | 接管现有中枢并启用自动发布 |
| `ops/fleet_update.py` | 拉取、健康检查、发布与失败回滚 |
| `ops/authorize_onedrive.py` | 管理员个人 OneDrive 一次性设备码授权 |
| `patches/`、`wheels/` | Relay Shell 清单热更新补丁与构建产物 |
| `licenses/`、`SOURCES.md` | 上游许可证、版本与来源 |

frp 下载包不提交 Git；生产环境保留 `/opt/fleet/app/downloads`。仓库中的空目录用于开发和测试。

## 本地验证

Python 3.12 或更新的兼容环境中，安装项目 wheel、FastAPI、HTTPX 和 pytest 后运行：

```bash
python -m pytest app/test_app.py app/test_oauth.py app/test_probe.py app/test_probe_api.py app/test_node.py app/test_node_metrics.py -q
```

MCP 集成和页面行为可分别定向验证：`python -m pytest app/test_mcp_entry.py -q`、`node --test app/test_dashboard.cjs`。这些测试使用合成数据、模拟服务或本机回环连接；页面检查不依赖 npm 包，也不替代真实浏览器验证。

中枢健康检查为 `/health`；实际节点状态、SSH 可用性和更新结果通过鉴权 MCP 或 WebUI 查看。

## HTTPS 与来源

当前部署使用可信 IP HTTPS 证书。Caddy 使用 HTTP-01 与 `shortlived` 配置自动管理证书；TCP 80 用于验证和 HTTPS 跳转，TCP 443 提供 WebUI 与 MCP。短期证书续期需要保留验证入口和 Caddy 状态目录。

上游：frp `0.71.1`、Relay Shell `0.4.0`（`a4e4835fb81f22d18208c468423ddf0d954ceb62`）、Caddy `2.11.7`。各自许可证与修改来源见 [SOURCES.md](SOURCES.md) 和 [licenses/](licenses/)。
