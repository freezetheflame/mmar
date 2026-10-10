# MMAR v0.1 优化评审

日期：2026-10-10。评审基线：`a4d756d`，Python 3.11.15。本文记录现有问题、实测证据和建议；下列运行时改进尚未实施。本次修改包括双语 README、评审文档和离线诊断脚本，未新增真实模型请求。

## 判断

继续使用 Python + asyncio + SQLite。30 Agent 实测的采样工作集峰值为 42.62 MiB，1,000 Agent 离线测试的闲置工作集约 30.89 MiB、后台 asyncio 任务为 2；现有证据没有显示重写为 Rust 的必要性。更值得优先投入的是异常生命周期、请求准入与观测，再处理模型上下文成本、查询效率和长期保留策略。

目前的成功实测是受控环形通信。它验证了发消息、接收、更新记忆、沉默和原子提交，不能证明开放式讨论会稳定收敛，也不能证明更高并发能够提高吞吐。

## 优先级

| 顺序 | 调整 | 证据 | 验收标准 |
|---|---|---|---|
| **P1 / 1** | 初始化和关闭的资源释放 | 离线注入导出超时后，Store 线程和锁仍在；Store 构造失败后 Adapter 未关闭 | 导出异常、初始化失败和重复关闭均可释放资源；同 Store 可以重新打开；清理结果可检查 |
| **P1 / 2** | 分开限速等待与请求超时 | 4 个 step 预留调用，只有 1 个进入 MockTransport，另 3 个因限速等待被标记 request_timeout | 单独记录准入等待、实际传输、step 总期限；低请求速率不被误判为远端超时；预算仍不可超支 |
| **P1 / 3** | 保留安全的响应与校验元数据 | 实测 3 次 JSON 修复成功，但停止原因、推理 tokens 和首次校验失败原因没有保留 | 可以区分截断、协议不合法、HTTP 失败与等待超时，记录修复次数和延迟；不存 key 或原始错误响应正文 |
| **P2 / 4** | 减少重复上下文与无效输出成本 | 63 次实测请求：41,459 输入 / 18,352 输出 tokens；短消息仍平均报告 291.3 个输出 tokens | 同一任务、相同调用预算下比较成功率、tokens、延迟和修复率；节约必须有实际测量 |
| **P2 / 5** | 大规模调度和目录构建 | 当前 claim 查询扫描 agents，并使用临时排序；创建会话对每位成员重建完整目录 | 比较 100 / 1,000 / 10,000 注册角色在稀疏激活下的 p50/p95 claim 延迟；公平性和跨会话串行不变 |
| **P2 / 6** | Store 历史增长与 metrics 成本 | 历史无自动归档；metrics 每次遍历所有已知 usage；多个计数在预算重置后的统计范围不同 | 明确当前预算与历史累计指标，提供关闭后的归档及容量指标，恢复和事件游标仍正确 |
| **P2 / 7** | 更接近实际协作的实验 | 环形测试由身份约束指定目标；当前目录主要暴露 ID | 加入不同角色、广播、多轮互动、背压和开放讨论，报告目标完成度与结束原因 |

## 1. 生命周期：已复现的两个资源释放缺口

### 1.1 导出异常会中断关闭清理

位置：[Runtime.close](../mmar/runtime.py)。当前顺序为 stop → 等待 exporter → flush_exports → model.aclose → Store.close。仅 model.aclose 这一段被 Store.close 的 finally 包住。`flush_exports()` 超时或其他异常会提前退出，跳过后面的资源释放。

离线复现向 `flush_exports` 注入 TimeoutError：close 抛出，`runtime._closed` 为 false，Store 工作线程仍活着，同路径再次打开得到 `store_in_use`。这是异常路径复现，不表示原先的 30 Agent 测试曾发生导出超时。

建议将资源释放统一放入有界且共享的关闭任务，并在清理后报告导出异常；并发或重复 close 应等待同一个任务。仅在已停止模型执行并结算在途 Store 命令后释放 Store，不能为了迅速关闭绕过停止屏障。清理必须处理调用者取消，同时避免永久等待不合作的自定义 Adapter。

### 1.2 Store 构造失败没有关闭传入的 Adapter

位置：[Runtime.open](../mmar/runtime.py)。`Store(...)` 位于 ready() 的异常清理范围之外。目录创建失败或 `store_in_use` 等同步构造错误时，函数退出而未调用 model.aclose。

离线诊断用可追踪的 Mock Adapter 复现：Store 已被占用时，open 被拒绝，而 Adapter 的关闭标记保持 false。当前 Runtime 会在正常 close 和 ready() 失败时关闭 Adapter；建议在文档中明确所有权并让所有初始化失败路径一致释放资源。

## 2. 请求限速、预算与超时需要分层

位置：[Runtime._step](../mmar/runtime.py) 和 [ChatCompletionsModel.complete](../mmar/http_model.py)。Runtime 先预留调用预算，接着对 complete 设置 request_timeout；complete 内部才等待共享限速器。等待限速锁和发起 HTTP 的等待时间都占用这个期限。

离线配置：4 个并发槽位、1 请求/秒、request_timeout=0.2 秒、每 step 1 次尝试，HTTPX MockTransport 立即响应。结果：4 次预留、1 次实际进入传输、1 个 committed step、3 个 request_timeout。此处的短超时用于清晰复现；一般情形是准入排队时间超过配置的请求期限，例如并发与请求速率相差很大时。预算对尝试保守计数是既有设计，问题是等待与传输的语义混合，并不是已经发生资金超支。

建议区分三个阶段：有界准入等待 → 持久预算预留 → 有界实际请求，并由 step 总期限覆盖全过程。若 Adapter 需要提前准入，可通过可选接口或共享准入组件提供；已有自定义 Adapter 应保持兼容。取消、暂停、stop 与崩溃之间仍须验证“预留不退款、旧结果不可提交、总调用不超预算”。

429 重试也需要共享冷却，而不是每个 step 都在 0.25 / 0.5 秒后立即重新竞争。后续可以按提供方支持情况尊重有界 Retry-After、使用带抖动的退避，并记录等待时间。对每个错误仍应设置次数与总期限上限。

## 3. 先让输出失败可解释，再调整 token 参数

位置：[ModelReply](../mmar/model.py)、[HTTP Adapter](../mmar/http_model.py)、[Runtime._step](../mmar/runtime.py)、[Store._finish_attempt](../mmar/store.py)。

现有 Adapter 只保留 content 和 usage；Store 进一步仅保留 prompt_tokens、completion_tokens、total_tokens。HTTP 状态归入宽泛错误代码，finish_reason 与嵌套 reasoning/cache token 计数未保留。首次 JSON 校验失败后会进入一次修复，但成功 step 没有专门记录失败类别和修复事件。

因此：实测的 3 次修复确实存在，但不能据此断言都是 512-token 上限导致截断，也不能把 18,352 个输出 tokens 都当作可见正文或都当作推理消耗。连通性调用曾返回 reasoning_tokens，但主测试未保存这些嵌套计数。

建议增加有界、白名单式元数据：HTTP 状态、finish_reason、响应字节数、数值型 reasoning/cache tokens、准入和传输耗时、校验失败类别、repair 标记。不默认保存原始响应、原始错误正文或完整失败输出。持久化新字段前应定义 schema 迁移与旧数据库兼容策略。

在观测完整后，再对模型支持的能力进行显式配置：JSON mode、严格结构化输出、推理设置和输出上限。兼容 Chat Completions 不等于支持所有参数，不能对所有提供方统一发送新增字段，也不应自动发付费请求探测能力。

## 4. 成本优化应逐项做 A/B 对比

当前实测平均每次调用报告 658.1 输入 tokens、291.3 输出 tokens。基于 SQLite 的不可变 step manifest 和文本版本，重建了 60 个 step 的上下文：平均约 2,597.9 UTF-8 字节；其中身份约 544.0 字节、目录 360.0、消息 195.1、历史 186.8、记忆 65.2。每项包含自身 JSON 包装；字段总和不等于完整上下文大小，字节也不等于模型 tokens。数据见 [上下文测量](benchmarks/review-measurements-v0.1.json)。

建议的比较顺序：

1. **缩短身份和公共协议。** 保留 schema、安全边界及通信规则，将重复的任务描述压缩为明确条件。检查成功率和自主目标选择是否下降。
2. **按实验需要配置历史。** 比较 history_messages=0 / 2 / 8；记忆已充分概括的任务可能无需重复历史，但多轮讨论需要上下文。不得默认删除历史语义。
3. **减少重复记忆写入。** 若 memory_replace 与现有文本完全一致，可考虑避免生成新文本版本和文件导出；step 决策与输入消费仍要保存，原子提交保持完整。
4. **显式配置推理和结构化输出能力。** 必须由所用模型实际支持并经过验证；更小输出上限也可能提高修复率，不能只看单次 token 数。
5. **利用批处理。** 多条待处理输入合并为一次 step 可以减少模型调用；需要验证消息顺序、广播回声、延迟和实验质量。环形测试使用 batch_size=1 是为了分别验证两个阶段，不能当成高效默认配置。

建议保留原始配置，逐次只改变一个因素。每个比较使用新 Store、相同 Agent 数和任务、明确调用与 token 上限；记录成功率、tokens、修复率、p50/p95 延迟和内存。成本优化的收益当前尚未实测。

## 5. 本地调度和 SQLite：按证据改进

在实测数据库上对 claim SQL 执行 EXPLAIN QUERY PLAN，当前主要计划为 `SCAN a`、相关子查询、对 deliveries 的 inbox_idx 查询，以及 `USE TEMP B-TREE FOR ORDER BY`。已有索引用于运行 step、收件箱和消息关联，但公平排序没有对应的排序索引。结果只能说明查询计划，不能单独证明这一查询占据主要耗时。

10,000 角色实验需要显式提高 directory_bytes、context_bytes 等容量限制，并单独记录这些配置；默认完整目录可能在建会话时就超过容量。该规模尚未验证，建议先用离线 Mock 测量本地调度，避免直接消耗真实模型配额。

可以先测量仅增加适合 active / blocked / turn / id 条件的索引是否有效，再比较从 pending deliveries 出发选取活跃候选。后者需要控制历史投递规模和避免重复 Agent。公平轮次和跨 session 串行是验收条件，不能为了速度牺牲。

`create_session` 和目录校验对每位成员重建其完整 peers；N 位成员时要处理约 N×(N−1) 个目录 ID。可考虑按会话成员版本缓存共享成员列表、复用序列化结果，或引入有界目录快照版本；注册、注销、成员变更和 group 权限变化都必须正确失效。默认目录的完整可见性应保留；邻居目录或角色筛选应是环境主动选择的实验策略。

Store 当前使用单线程连接、DELETE journal 和 synchronous=FULL，每条命令独立事务。模型响应很快时，提交与查询开销会更显著，但已有资源报告未分解 fsync 和 SQL 耗时。建议补充 Store 入队等待、SQL 执行和事务提交分布。WAL + FULL 可作为独立备选测量，只有在吞吐、恢复、归档和跨平台检查完成后才考虑默认变更。不要以降低持久性等级作为默认性能改进。

## 6. 长期运行与指标范围

`metrics()` 遍历所有已有 attempt usage 并在 Python 中汇总，因此持续运行后每次读取的成本随历史增长。可增加事务内维护的历史累计计数，并独立报告当前 budget epoch 的计数；不能靠删除历史或预算重置让消耗记录消失。

现有 reset_budget 仅重置预算计数；step_states、usage_known_attempts 和 reported_tokens 仍统计历史。用户可能把当前 calls 与历史 tokens 当成同一窗口。需要明确区分预算窗口和历史累计，并给新记录保留预算 epoch 或可查询的归属。实现前应准备 schema 迁移、旧 Store 启动验证和故障恢复测试。

长期历史保留需要定义：哪些会话已终止、哪些事件消费者游标仍需读取、哪些文本版本被 step 引用、哪些恢复输入仍需要保留。先提供关闭后的备份与整 Store 归档、磁盘容量指标；如果做局部清理，必须保留引用一致性和游标边界，不能直接删除旧 texts 或 events。

## 7. 下一轮实验与工程顺序

建议按以下顺序推进，每一阶段独立提交、保留基线：

1. **可靠性小迭代：** 初始化 / close 清理回归测试，修复资源生命周期；分开准入等待与请求超时；加入安全元数据及 repair 事件。运行现有 47 项测试，并新增对应的异常和取消测试。
2. **有限成本实验：** 先保持 30 Agent、并发 4，再逐项比较身份、历史、批量输入和模型支持的参数。每轮明确独立预算，不根据一次环形成功直接提高并发。
3. **资源与长期运行迭代：** 稀疏激活、持续追加历史、多会话公平性、大广播背压、Store 计数与磁盘归档，必要时再评估 WAL 和索引。
4. **协作质量实验：** 提供角色介绍与有意义的目标选择信息，区分研究员、批评者、整理者等身份；加入多轮自主交流与群组广播。由环境定义完成条件、轮次或期限，分别报告目标达成、idle、预算耗尽和超时。IDLE 只表示当前没有待处理消息，不等于任务目标完成。

当前无需为了这些迭代引入大型 Agent Framework、向量数据库或多进程系统。内核继续保持通信、调度和持久化职责，任务规则与角色策略留在环境侧。

## 复现与证据

```bash
uv sync --frozen --extra api
uv run --no-sync python -m benchmarks.review_checks --output .mmar/review-checks.json
```

离线诊断使用 HTTPX MockTransport 与注入异常，消耗 0 次提供方请求；结束前显式清理复现时打开的资源。它是用于观察已知边界的脚本，修复后 reproduced 字段可以变为 false，不应当作接受缺陷的单元测试。输出快照见 [离线复现结果](benchmarks/review-checks-v0.1.json)。

评审来源：[30 Agent 实测](live-provider-validation.md)、[实测原始数据](benchmarks/live-intern-30.json)、[离线资源数据](benchmarks/mock-scale.json)，以及当前仓库中的 Runtime、Store、ModelAdapter 和 HTTP Adapter 实现。所有建议中未测量的收益都仍是待验证假设。
