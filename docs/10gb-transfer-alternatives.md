# 单次最大约 10GB 的备用传输选择

核查日期：2026-10-16（北京时间）。用户确认单次最大约 10GB；本轮仅核查公开文档与源码，没有改生产配置、部署服务、读取凭据或上传文件。

## 推荐顺序

不是没有替代方案。先区分两端同时在线的转发，与发送端先上传、接收端以后下载的暂存。前者不需要云端 10GB 空间，后者需要存储服务及清理恢复机制。

| 路线 | 数据路径和容量 | 适用场景 | 主要代价 |
| --- | --- | --- | --- |
| Cloudflare 私网 Tunnel + One Client | 本地 → Cloudflare 私网 → 训练节点；不占 R2 存储 | 两端同时在线，希望避开原 XTCP 打洞 | 本地客户端、训练端 cloudflared、Zero Trust 组织与访问规则；免费计划设置文档仍要求支付信息；没有实测吞吐 |
| R2 滚动分块 | 本地 → R2 小对象 → 训练机；确认落盘即删，可将同时存储控制在数 GB | 希望受支持的 HTTP API、可重试及恢复，同时降低暂存需求 | R2 计费账户；需要实现分块/确认/清理，训练机离线时达到容量上限就暂停上传 |
| croc 直接或公共中继 | 本地 → 对端，或本地 → 公共 relay → 对端；无需暂存整个文件 | 不想先建对象存储，希望快速试验现成 CLI | 两端在线；公共 relay 不是容量、速率和长期可用性承诺，不适合作为保证高速的唯一通道 |
| 自有独立 relay | 本地 → 有足够出口的自有机器 → 目标 | 已有另一台公网大带宽机器，重视可控性 | 使用那台机器的流量/出口；放回当前小带宽中枢会重现原瓶颈 |

## R2 原额度的澄清

10 GB-month 是按每日峰值平均计算的月存储量，不是每月只能传 10GB。上传 10GB 后训练机下载同一对象，不会仅因一上传一下载就占 20GB 存储；完成删除后可以继续周转。请求次数、对象重叠、未完成分片也需要计入预算。出口免费不等于所有操作或超额存储免费。

可以把一个 10GB 文件拆成例如 256MiB–1GiB 的独立不可变对象，使用有界窗口：上传几块 → 节点按顺序写入 `.part` 并持久确认 → 删除这些对象 → 上传后续块。这样不用同时保存整份 10GB。该方式使用正常 PUT/GET/DELETE 接口，不依赖规避限额；上限前必须反压，不能在接收端长期离线时无限积压。

恢复记录需包含任务、块编号、长度、对象标识及目标已确认偏移；删除必须晚于持久确认，整文件完成才原子改名。清理失败的对象仍占额度，必须保留记录重试。使用实际字节数设置容量余量；10GiB 与十进制 10GB 不是同一数值。此方案尚未实现。

官方：[R2 价格](https://developers.cloudflare.com/r2/pricing/)、[固定文档源码](https://github.com/cloudflare/cloudflare-docs/blob/f775315898df547b0dd049879f084523df136a33/src/content/docs/r2/pricing.mdx)、[单位及大小限制](https://github.com/cloudflare/cloudflare-docs/blob/f775315898df547b0dd049879f084523df136a33/src/content/docs/r2/platform/limits.mdx#L35)。详细 API 与恢复边界见 [R2 报告](cloudflare-fallback.md)。

## Cloudflare 公共域名与私网必须分开

[官方 Tunnel FAQ](https://developers.cloudflare.com/cloudflare-one/faq/cloudflare-tunnels-faq/#large-file-and-streaming-traffic-through-tunnel) 明确：公共 hostname 经过 Cloudflare 反向代理，在 Free/Pro/Business 上，提供视频和其他大文件须使用指定付费服务。随后明确指出 private network routes 不公开应用，此限制不适用，可由 Cloudflare One Client、Mesh 或 WAN 访问。[固定源码](https://github.com/cloudflare/cloudflare-docs/blob/f775315898df547b0dd049879f084523df136a33/src/content/docs/cloudflare-one/faq/cloudflare-tunnels-faq.mdx#L63)。

因此不推荐用公开 Tunnel + WebDAV 分片来规避大文件政策。可行的候选是授权私网：本地 One Client 接入，训练端 cloudflared 发布受限私网地址，文件通过现有 SSH/SFTP 进入训练机。中枢仍仅分配任务与记录状态。此时不用对象存储，也不是把训练机无鉴权暴露到公网。

需要区分两段网络：本地 One Client 的 MASQUE 文档有 TCP 443 的 HTTP/2 fallback；训练端 cloudflared 则需要 UDP 7844（QUIC）或 TCP 7844（HTTP/2）。只开放 443 的训练环境仍可能不满足后者。TCP 路径存在不等于已证明具体网络可通、断点能恢复或速度足够。现有 SFTP 上传器没有跨进程恢复，换私网后也不能声称自动获得这项能力。

管理端开通要求见 [官方设置说明源码](https://github.com/cloudflare/cloudflare-docs/blob/f775315898df547b0dd049879f084523df136a33/src/content/partials/cloudflare-one/choose-team-name.mdx#L10)：选择 Free 计划仍须填写支付信息，文档说明该步骤不收费。训练端端口见 [Tunnel 防火墙说明](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-with-firewall/)。本轮没有进入实际 checkout，也没有核实当前免费席位数。

私网方式适合本地和训练机同时在线。要维持客户只执行一次接入命令，Fleet 安装器可以在运营方完成组织、路由与权限配置后负责训练端接入；本地客户端的首次组织注册仍是额外步骤，不能隐去。

## croc / Magic Wormhole

已读取 croc 官方源码快照 `986f9776615c1b5a7ab2d368d34c471c9a7a8791`。[README](https://github.com/schollz/croc/blob/986f9776615c1b5a7ab2d368d34c471c9a7a8791/README.md#L13) 明确支持直连与中继回退、端到端加密、文件/目录和中断续传；[传输选择说明](https://github.com/schollz/croc/blob/986f9776615c1b5a7ab2d368d34c471c9a7a8791/README.md#L443) 也明确公共 DERP 是 best effort，可能受公平使用限制。支持自建 TCP relay。

这是无需网页解析或私有下载 API 的真实 CLI 备选，但没有核实公共服务对持续 10GB 自动化周转的额度或吞吐承诺。源码功能不是服务 SLA；选定稳定发行版后还要验证恢复行为、源文件变化、部分文件、出口连通和内存。不得把仅阅读当前主分支当作发行版或线上可用性实测。

Magic Wormhole 官方 [README](https://github.com/magic-wormhole/magic-wormhole/blob/cc6160670bf7faaab52ba307e3f8ba95e80529fe/README.md#L13) 支持任意大小文件；[Transit 文档](https://github.com/magic-wormhole/magic-wormhole/blob/cc6160670bf7faaab52ba307e3f8ba95e80529fe/docs/transit.rst#L1) 说明直连失败后使用 TCP relay。它也可以作为候选，但本轮没有确认 CLI 跨进程续传契约，不把它列为 10GB 恢复能力已验证的首选。

## 本轮结论

单次最大约 10GB 时，不必为了容量寻找更大的匿名网盘。两端同时在线可优先验证 Cloudflare 私网或 croc；需要离线暂存、可恢复 HTTP 和受支持 API 时，R2 仍可用，并可用滚动分块降低同时存储量。若公共免费中继在实际地区吞吐不合适，真正可控的选择是独立自有 relay，而不是把流量重新压回当前中枢。

本轮只核查文档/源码；没有运行这些通道的端到端传输、验证账号开通或测试 10GB 吞吐。
