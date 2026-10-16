# MCP 后台任务、定时监控与 Codex 唤醒评估

审查日期：2026-10-16。本文是设计评估，未增加生产工具、定时任务或外部通知。

## 最值得关注的结论

1. **可以让工作在后台持续执行，并让 MCP 调用很快返回；这不需要 Codex 一直等待。** 现有 `ssh_spawn` 已返回会话 ID，`session_recv(timeout=0)` 可立即取已有输出。但它是内存中的 SSH PTY 会话，不是可跨中枢重启的持久作业系统。
2. **MCP 本身不承诺主动唤醒当前云端 Codex。** 事件送达客户端、客户端展示通知、客户端开启一个新的模型 turn，是三个不同能力。当前会话没有完成客户端握手或唤醒实测，不能承诺后两者。
3. **定时与监控应该由中枢的轻量任务调度器独立完成。** 模型只创建规则、查看结果、处理需要推理的异常。10 秒心跳、阈值判断、失败重试不应每次都唤醒模型。
4. **不要直接照旧版 MCP Tasks 教程实现。** 2025-11-25 的 Tasks 曾是实验性核心功能；2026-07-28 已重新设计为官方 `io.modelcontextprotocol/tasks` 扩展。当前安装的 Python MCP 2.3.0 保留旧任务类型，但核心不派发旧任务方法；Fleet 也没有接入任务扩展。兼容现有客户端的最小方案是普通 `fleet_job_*` 工具返回 JSON 作业 ID，再视真实客户端能力增加协议适配。

## 已有实现与边界

| 结论 | 代码依据 | 影响 |
| --- | --- | --- |
| `ssh_exec` 等待命令结束，默认超时 60 秒 | `.venv/lib/python3.12/site-packages/relay_shell/server.py:957`、`config.py:77` | 不适合持续训练、数小时数据准备；超时也不等于远端没有产生效果，不能盲目重试。 |
| `ssh_spawn` 创建 SSH PTY 后返回 ID | 同目录 `server.py:1006` | 只等建连与启动；不必等业务完成。初次 SSH 建连本身仍可能较慢。 |
| `session_recv` 默认等 2 秒，可设 0，最大 60 秒 | `server.py:1093`、`sessions.py:445` | 普通查询用 0；不要通过不断 60 秒读取使模型一直挂在等待。 |
| 会话可列举、取输出、发信号、关闭 | `server.py:1138`、`server.py:1172`、`sessions.py:523` | 已能完成短期后台交互工作，不需要重复开发同类工具。 |
| 会话存于进程内字典；默认闲置 1800 秒，可被清理 | `sessions.py:345`、`sessions.py:547`、`config.py:89` | 清理在 create/list 时触发，不是精确的 30 分钟定时器；空闲的长任务可能被后续调用清理。 |
| 关闭 MCP 会关闭会话和底层连接 | `sessions.py:562`、`relay_shell/__main__.py:477`、`app/mcp_entry.py:99` | 不能保证 SSH PTY 里的工作跨 MCP 更新存活。训练等长作业需远端独立进程管理和持久元数据。 |
| 当前 HTTP 是 `stateless_http=True`、`json_response=True` | `relay_shell/server.py:774`、`app/mcp_entry.py:205` | 当前未提供专门订阅长连接，不能假设已有可用的主动推送通道。 |
| Fleet 已有独立后台巡检与 SQLite 数据 | `app/app.py:37`、`app/app.py:127`、`app/app.py:170`；`app/probe.py:182` | 可复用数据库和巡检快照构建规则，不必新增 Redis、Celery 或每条规则一个线程。 |
| 监控工具读已存数据，不额外 SSH | `app/mcp_entry.py:150` | `fleet_monitor` 已能减少模型为观察状态而反复执行远程命令。 |

短任务的现成调用顺序：`ssh_spawn(host=节点ID, command=命令)` → 保存返回 ID → 做其他工作或结束当前模型回复 → 下次用 `session_recv(session_id=ID, timeout=0)` / `session_list()` 查询；停止时用 `session_kill`。不要把这一流程描述为“下一轮 Codex 会自动被唤醒”。需要长期保存的日志应由远端作业写入受限目录；现有会话缓冲有上限且读取会消费输出。

## 官方协议与 Codex 支持的证据

- [MCP Tasks 当前总览](https://modelcontextprotocol.io/extensions/tasks/overview)：服务端在客户端明确支持扩展后，可以对原调用返回 `resultType: "task"`；`tasks/get` 返回状态和最终结果，`tasks/update` 提交中途输入，`tasks/cancel` 请求协作取消。默认轮询；通知需要客户端显式订阅 `subscriptions/listen`。这仍不规定宿主何时启动新的模型推理。
- [官方 Tasks 扩展仓库](https://github.com/modelcontextprotocol/ext-tasks)：README 列 `2026-07-28` schema 为 Stable。普通 API 作业 ID 与协议 Task ID 可以映射，但没有必要为本项目先升级整个协议栈。
- [2026-07-28 变更记录](https://modelcontextprotocol.io/specification/2026-07-28/changelog)：旧 `tasks/result` 阻塞方法被移除，新扩展把结果放入 `tasks/get`；旧 `tasks/list` 也被移除。注意总览中“服务端可主动返回 task”指响应一次已有客户端请求时返回句柄，不是凭空启动用户会话。
- [2025-11-25 旧 Tasks](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks)：旧版 `tasks/result` 在未完成时必须阻塞，`notifications/tasks/status` 可选且不能依赖。仅在兼容旧客户端时考虑此语义，不能与新版混用。
- 当前 `.venv/lib/python3.12/site-packages/mcp_types/_types.py:610` 明确：旧 Tasks 在 2026-07-28 移出核心；这里只保留类型，不进入请求/通知派发。Fleet `app/mcp_entry.py:133` 起只注册普通工具，没有 Task 扩展处理器。
- [Sampling 当前规范](https://modelcontextprotocol.io/specification/2026-07-28/client/sampling)：Sampling 在 2026-07-28 已弃用；需要客户端声明支持，是已有请求处理过程中的模型请求，不是通用定时唤醒接口。Elicitation 是向用户索取输入，也不应拿来伪造唤醒事件。
- [Codex 官方 CLI 通知处理源码](https://github.com/openai/codex/blob/main/codex-rs/rmcp-client/src/logging_client_handler.rs#L64)：当前 `on_progress`、资源更新、工具列表变化处理仅记录日志；[elicitation 处理](https://github.com/openai/codex/blob/main/codex-rs/rmcp-client/src/elicitation_client_service.rs#L203) 将交互请求交给 UI。已审查的路径未提供“通知到达就启动模型 turn”的行为。
- **以上 CLI 源码不是当前云端 Codex 的支持承诺。** 没有核实该云端客户端实际 capabilities、Tasks 扩展支持、闲置订阅维持或模型调度策略；官方客户端矩阵也未提供足以确认此组合的 Tasks 条目。
- [官方 Codex TypeScript SDK](https://github.com/openai/codex/blob/main/sdk/typescript/README.md)：提供 `startThread()`、`resumeThread(id)`、`thread.run()` / `runStreamed()`。若确需自动分析异常，可由用户控制的常驻宿主消费 Fleet 事件，再调用 SDK 开启/续接**该宿主管理的线程**；这不等于接管本次云端对话。需另行配置实际可用的认证、运行环境和费用策略，本次未安装或执行。

官方 GitHub 原始文档已实际下载并阅读。`developers.openai.com` 和 GitHub API 的当前访问被网络策略拒绝，因此没有据此确认 Codex App Automations 或云端唤醒功能。没有绕过访问限制。

## 建议的最小接口与实现顺序

第一步复用现有会话工具并增加持久任务能力，保持普通 MCP 工具兼容性：

| 建议接口（尚未实现） | 最小契约 |
| --- | --- |
| `fleet_job_start(node_id, argv, cwd, idempotency_key, run_at?, timeout_s?)` | 校验与审计后先写数据库再返回 `task_id`、`status=queued`、`next_poll_seconds`。不等待 SSH 建连或命令完成。初版可只接受固定操作类型，任意命令必须沿用原策略与审计，不能经后台绕过。 |
| `fleet_job_status(task_id, after_log_offset=0)` | 快速返回状态、时间、退出码、有限长度日志与下一偏移；未知/离线不冒充失败；不能把重复查询变成再次执行。 |
| `fleet_job_list(state?, cursor?)` | 找回历史/未结束任务，不依赖模型记住 ID。普通业务工具，不冒充新版协议已移除的 `tasks/list`。 |
| `fleet_job_cancel(task_id)` | 幂等提交取消意图；先显示 `cancel_requested`，确认远端退出后才变为 `cancelled`。未确认时不能宣称训练进程已停止。 |

可在第二步增加 `fleet_watch_upsert(rule_id?, node_id, metric, threshold, hold_seconds, cooldown_seconds)`、`fleet_events(after_id, limit)`；规则变更与停止应提供对应删除/禁用入口。初版只落库告警和显示，不擅自发送外部消息。周期命令可用同一 job 表配 schedules 表，先支持 UTC `run_at` 和简单间隔，避免一开始引入完整 cron 语法。

实现建议：

- 新 `app/jobs.py` 保存 jobs/schedules/events 表与有上限的 worker（例如同时 1–2 个 SSH 启动操作）；`app/app.py` 接管理员 API 并在 lifespan 启停；`app/mcp_entry.py` 仅做参数契约与已有 `audited()` 转接。监控规则读取已有探针缓存，不加新 SSH 轮询；SQLite 沿用短事务，不能在事务里等网络。
- 长作业交给训练机 `systemd-run` 或等价受控 runner，记录 unit/job ID 和有限日志；由中枢查询和取消。无 systemd 的环境需明确的轻量 runner 与持久退出码协议，不能拿 `nohup` 后丢弃 PID 当可靠完成。保留现有 SSH 连接池；不要让每个作业长期占用一个中枢 SSH PTY。
- 业务状态：`queued → starting → running → succeeded/failed`；附加 `cancel_requested/cancelled`、`unknown`。中枢重启后先核对远端作业 ID；若启动请求发出后结果丢失，标 `unknown`，用幂等键/远端任务标记恢复，不盲目重发有副作用的命令。
- 定时记录 `next_run_at`、`last_run_at`、租约/所有者；同一规则默认禁止重叠，错过多个周期合并一次，限制总并发与保留数量。训练机与中枢失联时保留现有工作，恢复后重新核对状态。
- 监控需“持续异常时长 + 冷却 + 恢复事件”，事件使用单调 ID 去重并持久化；指标过期是 unknown，不当作 0，也不等于设备故障。消费者重启可凭游标继续，避免大量重复唤醒与日志占满 1 GB 中枢。
- 若实际宿主支持新版 Tasks，再加薄适配层映射 job 状态、TTL、取消和事件订阅；普通工具继续可用。先完成真实 capabilities 与客户端断线/重连验证，再决定是否使用该适配。

## 可验证的验收条件

1. 使用隔离模拟 SSH：接受任务即返回 ID，目标延迟/失联不拖长提交调用；查询不执行远端业务命令。
2. 同一幂等键重复提交只启动一次；分别在“已落库未启动”“远端已启动但确认丢失”“运行中”重启 worker，不能重复执行。
3. 取消只作用于所记录的任务，晚到的完成消息不覆盖已确认终态；断连时可显示尚未确认。
4. 模拟时钟验证时区、漏过周期、互斥、去重、告警持续时长/冷却；过期指标不触发资源阈值告警。
5. 限制日志、任务保留期和输出分页，测小内存下常驻 RSS 与事件风暴峰值，不跑全量训练或读取真实凭据。
6. 单独实测用户实际 Codex 客户端：能力声明、任务支持、空闲事件接收、是否启动模型 turn；未通过前只承诺后台执行与下次主动查询。

## 本次实际验证范围

- 只读检查 Fleet、安装 wheel 的 Relay Shell 与 MCP SDK，读取官方 MCP 和 Codex CLI/SDK 源码。
- 用内存中的合成 Transport 实际运行 SessionRegistry：无输出 `recv(timeout=0)` 约 0.007 ms 返回；后续进度可读；shutdown 确实关闭并移除会话。此结果只验证会话库语义，不代表真实 SSH 延迟或云端客户端体验。
- 未接入真实节点、未建立用户 MCP 会话、未运行 Codex SDK、未创建调度任务或发送外部通知；持久 job/规则接口都是建议，尚未实现。
