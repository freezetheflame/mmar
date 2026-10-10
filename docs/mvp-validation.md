# MMAR MVP 验证记录

日期：2026-10-10。对应当前项目实现，架构基线为 v0.4。

## 交付范围

标准库核心、共享异步 Runtime、单线程 SQLite Store、结构化决策校验、Mock ModelAdapter、可选 HTTPX Chat Completions Adapter、CLI、三人文件示例、测试和资源测量脚本。

Agent 不具有自己的任务、队列、锁、客户端或工具执行能力。运行时只为正在处理输入的 Agent 创建 step Task。权威记忆与投递在同一数据库事务提交。

## 实际执行的验证

| 项目 | 命令或证据 | 结果 |
|---|---|---|
| Python 3.11.15 | `.venv/Scripts/python.exe -m unittest discover -v` | 47 项测试通过 |
| Python 3.12.2 | `python -m unittest discover -q` | 47 项测试通过 |
| 核心无第三方依赖 | `.venv/Scripts/python.exe -S -m mmar demo --agents 10` | 10 人讨论到达 IDLE，13 个 committed step、21 次投递 |
| 冻结依赖安装 | `uv sync --frozen --extra api` | 成功，8 个包 |
| 依赖兼容性 | `uv pip check --python .venv/Scripts/python.exe` | 全部兼容；此项不是漏洞扫描 |
| 安装后的 CLI | `uv run --no-sync mmar demo --agents 10` | 成功，输出 UTF-8 中文消息与统计 |
| 文件身份、记忆与群组示例 | `python -m mmar run examples/discussion.toml` | 三人讨论达到 IDLE，记忆导出成功 |
| 历史查询 | `.venv/Scripts/python.exe -m mmar inspect .mmar/discussion.db` | 返回已持久化的计数与四条消息，无模型调用 |
| 字节码编译 | `python -m compileall -q mmar tests benchmarks` | 成功 |
| 缺失 key 防护 | 子进程强制设置空 MMAR_API_KEY / MMAR_MODEL | CLI 明确失败，无模型请求 |
| 资源测试 | `python -m benchmarks.mock_scale --output docs/benchmarks/mock-scale.json` | 10、100、1,000 个 Agent 各完成一次 Mock 输入处理 |

## 必需能力与证据

| 能力 | 实现及测试 |
|---|---|
| 身份与持久记忆 | register、read_memory、import_memory、reload_identity；文件导出与版本冲突测试 |
| 单目标、多目标、广播与沉默 | Decision 校验、commit 展开；通信、fan-out、权限、空行动测试 |
| 自主通信目标 | Runtime 接受当前 session 中任意合法目标；真实 Adapter 提供完整目录，没有固定讨论流程 |
| 并发与同 Agent 串行 | 槽位前 Claim、active 映射、数据库唯一约束；同步屏障测试确认两个不同 Agent 可同时推理，跨 session 同 Agent 不重叠 |
| 动态注册注销 | register、unregister_agent、set_participants；动态目录与 tombstone 测试 |
| 预算与背压 | step/call/delivery 原子预留、inbox 和 Store 队列限制；争抢末次额度、重试计数、整轮回滚、内部背压测试 |
| 运行控制 | start、pause、resume、stop、stop_session；暂停、排空、取消、迟到返回和会话隔离测试 |
| 环境接入 | inject_message、activate、events、read_message；幂等冲突、游标续读、慢消费者、已接纳命令取消测试 |
| 可靠提交与恢复 | Claim / Reserve / Commit；重复提交及真实子进程 os._exit 故障测试 |
| 可观测性 | metrics、事件游标、步骤状态、已知/未知 usage 与原因；拒绝事件和 usage 过滤测试 |

强制进程退出测试覆盖：Claim 后、Commit 事务中记忆已写但输出尚未插入、Commit 后。检查重启后旧提交不重发、未提交事务回滚、恢复次数有上限、调用预算不退款。

HTTP 接口测试使用 HTTPX MockTransport，验证请求字段、key 不进入上下文、429 不隐藏重试、响应上限、HTTPS 限制和取消传播。没有实际远端请求。

## 本机资源结果

原始记录见 [mock-scale.json](benchmarks/mock-scale.json)。并发为 4，Mock 等待为 5 ms，SQLite 使用 FULL 同步级别。

| 注册 Agent | 闲置后台 asyncio 任务 | 闲置进程工作集 MiB | 运行秒数 | committed step |
|---:|---:|---:|---:|---:|
| 10 | 2 | 29.37 | 0.247 | 10 |
| 100 | 2 | 29.78 | 2.441 | 100 |
| 1,000 | 2 | 30.89 | 29.068 | 1,000 |

工作集包含解释器与 SQLite 原生内存，由 Windows 进程 API 测量；脚本同时启用 tracemalloc，其分配统计单独保存，不能当作总内存。规模测试在同一进程按顺序运行，且有本机其他负载；这些数据用于 MVP 检查，不是严格隔离的性能基准。真实 HTTP 客户端、TLS 和模型响应会增加资源占用。

1,000 个注册 Agent 不等于 1,000 个并行模型请求；本次所有规模都只有 4 个执行槽位。完整目录较大时会增加模型输入成本，不能据 Mock 吞吐推算真实模型吞吐或费用。

## 当前限制与下一次验证

- 尚未使用真实 key，提供方模型名称、JSON mode、token 参数、usage 和实际取消表现需接入后测试。
- token 指标是提供方已报告的数值，不是精确资金预算；没有 usage 的请求单独呈现。
- Agent 不自行定时唤醒，环境通过 activate 或消息触发。
- 同 ID 的记忆跨 session 共享，ID 在同 Store 中不复用。
- 历史版本和事件保留，尚未自动归档清理；长期实验需管理 Store 磁盘使用。
- 核心只有 Python API 和 CLI，尚无 Web 服务或图形界面。

结论：离线 MVP 和兼容 API 接口可以交付；下一步用真实提供方做小规模端到端试跑，采用有限请求预算和共享限速。
