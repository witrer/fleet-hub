# 个人 OneDrive 临时中转

核查日期：2026-10-16（北京时间）。适用于个人 OneDrive，包括 Microsoft 365 个人版、家庭版成员自己的 OneDrive。管理员配置一次，上传客户和训练机不需要各自注册 Microsoft 账户。

## 文件路线与“中枢直链”

```text
本地上传器 ── 分片 PUT ──→ OneDrive ── 文件 GET / Range ──→ 训练机
     │                         ↑                              │
     └── 任务、分片会话 ──→ Fleet 中枢 ←── 领取任务、进度、完成 ──┘
                              │
                              └── 鉴权后返回 302 下载地址
```

中枢保存任务和 OneDrive 元数据，通过 Microsoft Graph 创建上传会话、查询文件、更新访问令牌和清理中转文件。中枢不接受文件正文，也不把 OneDrive 下载响应读入内存后转发；小带宽中枢不会承载这两段文件流量。

稳定的中枢下载入口是：

```text
GET /api/relay/transfers/{task_id}/files/{file_id}/content
```

这个入口需要 Fleet 管理员授权或该任务所属节点的授权。中枢查询当前文件信息并返回 `302 Location`，客户端再直接请求 OneDrive 的短期预授权文件 URL。它不是网页分享链接，也不是永久公开链接。任务完成清理、取消或到期后不能继续用它下载。

训练端显式处理重定向：访问中枢时携带节点凭据，访问 OneDrive 时创建新请求，不带中枢凭据。续传的 `Range` 发给 OneDrive 文件 URL。不要用会把 `Authorization` 跨域转发的下载参数来替代这一步。

## 管理员一次配置

1. 准备自己可管理的 Microsoft Entra 应用注册。账户类型选 **Personal accounts only**，或 **Any Entra ID Tenant + Personal Microsoft accounts**；只支持组织账户的应用不能用于此配置。
2. 在应用的 Authentication 设置中启用 **Allow public client flows**。这里用设备码授权，不需要中枢回调地址，也不需要 client secret。
3. 使用 Microsoft Graph 委托权限 `Files.ReadWrite`，授权请求同时带 `offline_access`。记录自己的 Application (client) ID，不使用他人的、公开工具的共享 client ID。
4. 在 Fleet WebUI 的 OneDrive 设置中填写 client ID，点击登录，再打开显示的微软官方链接并输入设备码。授权完成后中枢保存配置并立即启用；客户不需要登录微软。也可以使用后文的管理员命令行工具。

注册应用是运营方的工作。微软当前注册应用快速入门列出的前置条件包括 Azure 活跃订阅、Entra 租户和应用注册权限；仅有个人 OneDrive 账户不代表已经有可用的应用注册环境。已有自己的 client ID 时不必重复注册。

### 推荐：网页授权

WebUI 显示登录等待、验证、成功、取消、拒绝或到期状态，以及当前配额和 Fleet 文件清理结果。关闭登录会话可取消后台轮询；浏览器隐藏时暂停页面轮询，后台授权仍有明确到期时间。网页只拿到短期用户设备码和微软登录链接，不接收 access token、refresh token 或 client secret。

授权 API 只允许 Fleet 管理员访问，启动与取消登录还检查同源 `Origin`。状态和设备码响应不缓存。中枢后台遵守微软轮询间隔、`slow_down` 和到期时间；配额查询采用独立短期缓存，日常 `/api/state` 不等待 Graph 网络请求。

成功后原子合并 `config.json` 的 `onedrive` 段并保存 0600 的 `onedrive-token.json`，保留其他配置。短期 0600 恢复日志保护两文件提交，启动时先恢复未完成的事务再加载配置。旧 provider 在刷新锁内失效，避免迟到刷新覆盖新令牌。不要把 token 或 `.onedrive-auth-*` 恢复文件纳入仓库。

有活动或待清理任务时，可重新授权同一 drive 和暂存目录，恢复失效凭据；不能换成其他账户或目录。每个任务另持久绑定 drive/目录，手动改配置也不能让旧任务误删新账号里的文件。重新授权会复用原暂存目录；初次连接使用 `FleetHubTransfers`。

网页相关 API：`GET /api/storage/onedrive`；`POST /api/storage/onedrive/login`（JSON `client_id`）；`GET /api/storage/onedrive/login/{session_id}`；`POST /api/storage/onedrive/login/{session_id}/cancel`。

### 可选：命令行授权

脚本只在管理员显式执行时联系账户；导入模块不会开始授权。现有默认部署的服务用户是 `fleet`，例如：

```bash
sudo -u fleet python3 /opt/fleet/repo/ops/authorize_onedrive.py \
  --root /var/lib/fleet \
  --client-id YOUR_APPLICATION_CLIENT_ID \
  --folder FleetHubTransfers
```

将脚本路径换成实际检出的文件路径。`--client-id` 必须是应用注册的 UUID。脚本使用 Python 标准库，无需安装额外授权工具。设备码轮询默认最多 900 秒，可用 `--timeout` 设置 1–3600 秒；服务端更早到期时按服务端期限停止。拒绝授权、错误码、临时网络错误与限流都有明确终止或有界重试处理。

脚本先查 `/me/drive`，精确查找或创建指定根目录下的专用文件夹，不枚举个人文件。确认成功后，原子写入：

```text
/var/lib/fleet/onedrive-token.json     权限 0600，持有 refresh_token
```

控制台只显示本次登录的设备码、剩余容量和配置片段，不显示 access token、refresh token、client secret。不要将 token 文件放进仓库。`Files.ReadWrite` 是账户文件读写权限；专用目录是 Fleet 的使用边界，并不是 Microsoft 将授权范围限制到该目录。

脚本不会覆盖已有 `config.json`。管理员将它输出的 `onedrive` 对象合并到现有配置顶层，保留其他键：

```json
{
  "onedrive": {
    "client_id": "YOUR_APPLICATION_CLIENT_ID",
    "tenant": "consumers",
    "drive_id": "DRIVE_ID_FROM_SETUP",
    "folder_id": "FOLDER_ID_FROM_SETUP"
  }
}
```

后续启动带有本适配器的中枢版本时加载配置。该部署步骤不由授权脚本自动执行。中枢按需刷新 access token，并将轮换后的 refresh token 原子保存；账户撤销授权或返回 `authorization_required` 时，管理员重新运行授权脚本。客户不需要处理这些令牌。

管理员应使用与服务相同的系统用户，保证中枢能读写这个 0600 文件。重新授权时应安排短暂维护窗口，避免正在运行的中枢刷新旧令牌与授权脚本写入新令牌竞争。

## 客户的使用方式

管理员配置完成、中枢和节点均更新到支持本通道的版本后，在现有上传命令上增加一个参数即可：

```bash
curl -fsSL https://YOUR_HUB/send.sh | bash -s -- \
  --node node-xxxxxxxxxx \
  --source ./dataset \
  --destination /data/dataset \
  --transport onedrive
```

PowerShell 使用 `-Transport onedrive`：

```powershell
& ([scriptblock]::Create((Invoke-WebRequest -UseBasicParsing 'https://YOUR_HUB/send.ps1').Content)) -Node node-xxxxxxxxxx -Source 'D:\datasets\dataset' -Destination /data/dataset -Transport onedrive
```

仍使用现有 Fleet 客户端授权与缓存。默认通道保持 `p2p`；目前必须显式选择 OneDrive，还没有“先等待打洞失败再自动切换”的策略。

上传器完成本地到 OneDrive 的阶段后就返回任务 ID 和状态查询入口，不等待训练机把全部文件下载完。`ready` 表示已在 OneDrive 暂存，`completed` 才表示训练机报告已持久落盘。训练机恢复联网后可在任务有效期内领取文件；客户上传完成后可退出。

## 容量、任务和恢复

当前任务默认上限为 **10 GiB**，清单最多 10,000 个文件和目录，默认最多 8 个活动任务，有效期 24 小时。10 GiB 约为 10.74 GB；账户实际可用空间需覆盖本批数据、同时进行的任务及其他网盘内容。创建任务前读取 Graph 剩余容量并考虑本系统尚未上传完成的预约字节；实际上传仍可能受其他客户端占用空间或 Microsoft 限流影响。此适配器不增加账户配额，也不保证线路速度。

清单还受 4MiB 元数据预算限制；大量长文件名可能先达到清单上限，需要拆成较少文件的任务。中枢创建时预留接收元数据开销，上传就绪时再次检查实际清单大小。节点须先通过心跳声明 `relay_protocol=1`，否则创建任务会提示等待自动更新，不会建立一个阻止旧节点升级的待收任务。

| 阶段 | 当前实现及边界 |
| --- | --- |
| 创建任务 | 客户端先持久保存幂等键，中枢保存任务、清单、上传会话和文件标识；请求重发复用同一任务。 |
| 分片上传 | 直接 PUT 到 Graph 返回的 `uploadUrl`，不附带 Graph 或 Fleet Bearer 凭据；分片按顺序提交，遵守小于 60 MiB、非末片为 320 KiB 倍数的官方要求。 |
| 上传重试 | 失败后先询问会话缺失范围，处理 416、限流与结果不确定的情况；不会假定超时一定代表上一片未收到。 |
| 上传进程中断 | 保留本机任务记录，使用相同来源和目标重新运行命令；按服务端已收偏移恢复。源文件大小或修改时间变化时拒绝复用活动任务，避免混传。 |
| 会话过期 | 中枢可重建会话。OneDrive 已清理的未完成片段无法恢复，需要重传该文件；重建次数有界。已完成文件由中枢记录复用。 |
| 节点下载 | 单独的有界内存后台线程下载，心跳不等待整个文件；临时文件和恢复记录落盘，完成后原子提交目标文件。 |
| 下载 URL 到期 | 再访问中枢鉴权入口获得当前地址；中枢核对文件标识、大小、名称和 eTag。 |
| Range 续传 | 直接请求 OneDrive 并检查 206 与 Content-Range；若服务端忽略 Range 返回 200，则清空临时文件后重下，不把整文件追加到已有片段。 |
| 接收进程中断 | 依靠绑定任务、文件版本和目标路径的临时文件恢复；落盘后、确认前中断也有提交记录。不能用另一个任务的部分文件冒充本任务数据。 |
| 终止与清理 | `completed`、`failed`、`expired`、`cancelled` 进入清理，对任务绑定的完整文件调用 `permanentDelete`，不先移入回收站；未完成上传会话单独取消，失败意图持久保存并重试。 |

状态主路径为 `uploading → ready → downloading → completed`。临时断网不等于立即失败；达到任务有效期后会过期。失败、取消或过期的任务需要新任务，不能保证继续复用旧部分数据。默认不对 10GB 数据反复做整文件哈希；大小、修改时间、eTag 和范围检查也不等于端到端密码学完整性证明，传输期间应保持源数据不变。

## 自动永久清理与历史回收站

新任务进入终态后，中枢通过官方 `POST /drives/{drive-id}/items/{item-id}/permanentDelete` 直接永久删除已绑定的 Fleet 临时文件。该操作成功后文件不可恢复，也不会先进入回收站；它解决新中转文件持续积累回收站的问题。清理范围只包括本任务记录的对象，不枚举、清空整个网盘或回收站，也不删除无关个人文件。普通 `DELETE driveItem` 仍是移入回收站，其语义没有被改变；永久清理失败时不会降级使用它。

调用前重新读取活动文件，检查原任务绑定的 item ID、名称、eTag、drive 及专用目录。检查发现其他程序修改、重命名或移动了文件时，保留待清理状态并报告 `object_changed` 或相应错误，不继续删除。请求还携带原 eTag 的 `If-Match` 作为防御，但微软没有在 `permanentDelete` action 的官方文档中保证支持这一条件头。读取检查到执行操作之间，外部修改仍有未验证的并发窗口，不能声称此路径具备远端原子条件删除保证；应将专用中转目录交给 Fleet 管理。

`cleanup_pending` 表示尚有清理操作待处理。失败意图和安全错误码持久保存，服务重启后继续处理；各任务按独立的下次尝试时间轮转，单个失败任务不会一直挡住其他任务。对已经绑定的文件，只有明确成功的永久删除调用才记为已清理。GET 或永久删除 action 返回 404，只能说明当前无法取得活动对象，也可能是文件已被移入回收站，不能记为已永久清除；这类情况保持待处理并报告 `object_unavailable`。成功响应也不保证账户配额立即刷新，后续仍以 Graph 的实际剩余容量为准。

对于以前通过普通 DELETE 移入回收站的历史文件，本次未核实到能够依据 Fleet 原有绑定、安全清理个人账户历史回收站的可靠官方接口。历史条目需要管理员到 OneDrive 回收站人工核对处理；中枢不会扩大范围清空其他文件。部署前仍需用授权账户的小文件验证永久删除、条件头实际行为和容量恢复时机。中枢直链用于临时周转，不是永久归档入口。

## 涉及文件

| 文件 | 职责 |
| --- | --- |
| `ops/authorize_onedrive.py` | 管理员设备码授权、专用目录与安全 token 保存。 |
| `app/onedrive.py` | Graph 元数据适配、刷新令牌、上传会话、下载地址和删除。 |
| `app/onedrive_auth.py` | WebUI 设备码登录、配额缓存、原子配置、同账户重授权和恢复日志。 |
| `app/relay_transfer.py` | 持久任务、鉴权、幂等、节点分发、302 入口、状态和清理。 |
| `app/app.py` | 注册通道、中枢生命周期、状态及心跳集成。 |
| `app/static/transfer.py` | 本地分片直传及进程重启后的恢复。 |
| `app/static/node.py` | 节点后台下载、Range、落盘与确认。 |
| `app/static/send.sh` / `send.ps1` | 将现有启动器的通道参数传给上传器。 |

## 官方依据与验证范围

本轮读取的是微软官方文档仓库中的当前文档源码，不依赖第三方网页抓链工具：

- [设备码流程](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code)：明确 `/consumers`、个人账户、用户登录流程、轮询错误码及 `offline_access` 返回 refresh token；[官方源码](https://github.com/MicrosoftDocs/entra-docs/blob/main/docs/identity-platform/v2-oauth2-device-code.md)。
- [注册应用](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app)：个人账户支持与注册前置条件；[官方源码](https://github.com/MicrosoftDocs/entra-docs/blob/main/docs/identity-platform/quickstart-register-app.md)。
- [启用公共客户端流程](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-configuration#enable-public-client-flow)：设备码流程所需应用设置；[官方源码](https://github.com/MicrosoftDocs/entra-docs/blob/main/docs/identity-platform/scenario-desktop-app-configuration.md)。
- [创建文件夹](https://learn.microsoft.com/en-us/graph/api/driveitem-post-children?view=graph-rest-1.0)：官方[权限表源码](https://github.com/microsoftgraph/microsoft-graph-docs-contrib/blob/main/api-reference/v1.0/includes/permissions/driveitem-post-children-permissions.md)列出个人账户委托 `Files.ReadWrite`。
- [创建上传会话](https://learn.microsoft.com/en-us/graph/api/driveitem-createuploadsession?view=graph-rest-1.0)：分片、偏移、会话到期、恢复和未完成上传清理；[官方源码](https://github.com/microsoftgraph/microsoft-graph-docs-contrib/blob/main/api-reference/v1.0/api/driveitem-createuploadsession.md)。
- [下载文件](https://learn.microsoft.com/en-us/graph/api/driveitem-get-content?view=graph-rest-1.0)：预授权直链、302 与 Range；[官方源码](https://github.com/microsoftgraph/microsoft-graph-docs-contrib/blob/main/api-reference/v1.0/api/driveitem-get-content.md)。
- [删除文件](https://learn.microsoft.com/en-us/graph/api/driveitem-delete?view=graph-rest-1.0)：移入回收站；[官方源码](https://github.com/microsoftgraph/microsoft-graph-docs-contrib/blob/main/api-reference/v1.0/api/driveitem-delete.md)。
- [永久删除文件](https://learn.microsoft.com/en-us/graph/api/driveitem-permanentdelete?view=graph-rest-1.0)：直接永久删除、不经过回收站，成功返回 204；请求头文档未保证 `If-Match`；[官方源码](https://github.com/microsoftgraph/microsoft-graph-docs-contrib/blob/main/api-reference/v1.0/api/driveitem-permanentdelete.md)。对应[官方权限表](https://github.com/microsoftgraph/microsoft-graph-docs-contrib/blob/main/api-reference/v1.0/includes/permissions/driveitem-permanentdelete-permissions.md)列出个人 Microsoft 账户委托 `Files.ReadWrite`。

本轮实际完成的隔离验证如下，均使用合成数据、合成令牌和临时目录：

- 真实上传程序、Uvicorn/FastAPI 中枢和节点接收器连接独立的模拟存储 HTTP 服务，2 项端到端测试通过。覆盖目录、空目录、空文件、多分片文件及单文件；落盘字节一致，中枢文件正文流量为零。清理改动后再次验证任务与三端合同，27 项相关测试通过。
- 中枢、网页登录、Graph 适配器与三端合同的相关测试通过；包括真实 FastAPI 路由发起设备码登录、后台完成配置保存、运行 provider 切换及旧 provider 失效。配置写入、日志删除或最终 fsync 失败的模拟均检查未激活新账户并恢复原配置。
- 永久清理测试验证不降级普通删除、绑定变化或对象消失时保持可见待处理状态，以及任务之间、同一任务文件之间的失败轮转。网页 23 项 DOM 模拟测试通过，覆盖网络展示、登录轮询与取消、官方链接及清理范围。
- 接收器、上传器、两端启动器和命令行授权相关测试共 94 项及 8 个子测试通过，覆盖 Range、短链过期、进程中断、最终响应丢失、限流、撤销、文件版本变化与进度请求临时失败。授权测试只使用合成令牌，验证 0600 原子保存和敏感响应不进入输出。

这些是与改动相关的针对性检查，没有运行全项目测试。模拟存储验证了程序之间的接口和恢复逻辑，未验证微软服务的实时行为。

尚未进行真实 Microsoft 账户授权、真实 OneDrive 上传下载、10GB 吞吐、回收站容量恢复或 Windows 执行验证。本轮没有读取真实凭据、手动修改生产配置或执行服务器部署；GitHub 推送后自动更新是否完成，应以服务器更新状态为准。官方 API 支持与模拟测试通过不能替代这些真实线路验证。

### 后续上传会话修正（2026-10-16）

用户随后提供的线上对照结果表明：创建上传会话时携带 `item.fileSize` 返回 `400 invalidRequest`，同一请求去掉该字段返回 200。对应的约 499 MiB 数据集任务已创建，但上传量为 0，目标文件尚未到达训练机；这只验证了会话请求的差异，不代表文件传输已经成功。

适配器现已移除请求中的 `item.fileSize`，保留内部文件大小校验、任务容量检查和每次分片 PUT 的 `Content-Range`。之前的模拟测试错误地要求该字段存在；回归测试现模拟携带该字段时返回 400，并覆盖小文件及 499 MiB 的会话元数据，不生成或上传大文件。另有针对性测试验证会话创建失败后，同一任务可以重新申请会话。

修复上线后，若任务尚未到期、源文件与来源和目标路径未改变，可重跑原上传命令复用任务。线上完整恢复仍须确认训练机报告 `completed` 且文件已落盘；本次对照实测来自用户反馈，本地回归使用合成服务，未直接访问真实 Microsoft 账户或 CANN。

### 上传中占位对象与断点恢复修正（2026-10-16）

用户继续实测后报告：已上传约 50 MiB，再次获取会话时，OneDrive 中同名的 0 字节占位对象被旧逻辑当作完整文件校验，导致 409。恢复现优先查询已保存的上传会话，按服务端 `nextExpectedRanges` 继续；不因本地缓存的过期时间提前新建会话，因为分片 PUT 可能已延长服务端会话期限。按名称查找文件也只读取元数据，不为占位对象获取下载内容。

只有会话明确返回 404/410 后才检查文件是否已完整提交，以恢复最终分片响应丢失的情况。名字、大小、eTag 校验仍然保留；若会话已失效且只剩占位或大小不符的对象，仍拒绝将其标为完成，也不会自动覆盖或删除。限流、临时服务故障和鉴权错误不会触发会话重建。

针对性回归覆盖 499 MiB/50 MiB 的恢复元数据、缓存过期、会话失效和错误分支。另用约 640 KiB 合成数据经过真实上传器、中枢和节点接收器：首片落盘后模拟占位对象及响应丢失，恢复原会话继续上传，验证首片未重传、节点最终字节一致、中枢不转发文件正文。这些是隔离测试；线上 499 MiB 数据集是否成功到达 CANN 仍需重试确认。
