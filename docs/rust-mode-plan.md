# MMAR Rust 模式：实现路线与兼容契约

日期：2026-10-11。状态：实现准备与可行性评估，尚无可运行 Rust 后端。当前机器未找到 rustc、cargo 或用户目录中的 Rust 工具链；本轮交付资源清理修复、共享协议用例与本文，不安装全局工具链，也不宣称未测量的性能收益。

## 迁移目标

最终目标为**整体迁移到 Rust**，这是用户在 2026-10-11 确认的方向。实施阶段先交付独立 Rust 可执行后端，暂时保留 Python 实现作行为对照；完成验收后由 Rust 接管默认运行与后续维护。任务规则、Agent 身份、记忆和模型决策协议继续保持一致。第一期使用 CLI 和离线 Mock，不同时改变调度算法、数据库 schema、预算语义或提供方设置。

独立后端能够验证单可执行文件部署、解释器启动开销与本地调度成本，同时避免第一期就把 CPython ABI、GIL、Python 异步事件循环和 Rust 异步生命周期绑定在一起。这里是架构取舍，不是已经证明独立后端更快。

| 形态 | 适用方向 | 当前选择 |
|---|---|---|
| Rust CLI / 独立后端 | 单可执行文件运行、本地服务、低闲置开销 | 先实现并独立验证 |
| Python → Rust 子进程桥接 | 兼容仍需要 Python 的宿主脚本 | 有明确宿主需求时再加，不作为整体迁移的前置条件 |
| PyO3 原生扩展 | 保持进程内 Python API，对已测出的热点加速 | 暂不作为第一期；需要额外 ABI 和生命周期验证 |
| 整体切换到 Rust | Rust 负责 Runtime、Store、模型接口与 CLI，实现统一维护 | 最终目标；通过完整兼容与恢复验收后切换 |

过渡期可提供 `--backend python|rust` 作为宿主选择；该参数当前尚未实现。两种后端不能同时写同一 Store。切换后 Python 只保留为历史参照或按需兼容层，不继续维护两套核心实现。

## 模块映射与依赖候选

建议 Cargo workspace 分为 protocol、core 与 CLI；HTTP Adapter 为可选 feature。离线构建不引入 HTTP/TLS 依赖，开发用例不请求真实模型。

| Python 模块 | Rust 职责 | 候选技术 |
|---|---|---|
| protocol.py | Decision、Action、Limits、ID 与 UTF-8 大小校验 | serde / serde_json；显式验证与重复键拒绝 |
| store.py | 独占锁、单连接、事务、预算、恢复、事件 | rusqlite + 一个专用 std::thread |
| runtime.py | 共享调度、在途任务、停止屏障、导出生命周期 | Tokio；默认先评估 current_thread executor |
| model.py | Mock 与模型调用边界 | 明确的异步 Adapter 接口，保留一次调用一次请求的语义 |
| http_model.py | 共享 HTTP 客户端、TLS、期限、限速与响应边界 | 可选 reqwest / rustls |
| cli.py | demo、run、inspect 与实验结果 | clap；TOML 配置解析 |

rusqlite Connection 为 Send、非 Sync，适合让一个工作线程持有连接；不要把连接直接放到异步执行器上跑阻塞 SQL。[rusqlite 官方文档](https://docs.rs/rusqlite/latest/rusqlite/struct.Connection.html)

长期 SQLite 工作循环使用专用线程；短文件写入才考虑有界阻塞池。Tokio 明确说明已经开始的 spawn_blocking 任务不能被 abort，因此切换到 Rust 也不能安全地强杀阻塞中的文件写入。[Tokio blocking 文档](https://docs.rs/tokio/latest/tokio/task/fn.spawn_blocking.html)

HTTP 连接池继续全局共享，显式设置 TLS、重定向、代理、响应大小和重试策略；不得默默继承与 Python 版不同的传输默认行为。[reqwest ClientBuilder](https://docs.rs/reqwest/latest/reqwest/struct.ClientBuilder.html)

使用 std::fs::File::try_lock 可以减少独占锁依赖；该接口从 Rust 1.89 起稳定。不过选定 crates 也可能要求更高 MSRV，实际最低工具链应在创建 Cargo 工程时统一验证。[Rust File 锁文档](https://doc.rust-lang.org/std/fs/struct.File.html#method.try_lock)

依赖版本须在工具链可用后选择稳定 release、锁定 Cargo.lock、禁用不需要的默认 feature，并完成 Windows/Linux 编译。是否用 bundled SQLite 要同时衡量系统依赖、编译工具链和 SQLite 版本一致性；此处不提前写未经构建验证的 Cargo 配置。

## 跨语言必须一致的语义

1. **Agent 是数据。** 闲置角色没有自己的 Task、线程、Queue、客户端或后台循环；最多 concurrency 个在途 step，同一 Agent 跨 session 串行。
2. **严格解析。** actions 与 memory_replace 为仅有的顶层字段；未知字段、重复 JSON 键、非法 Action、重复 targets 和非法 ID 拒绝。先反序列化为通用 Value 再转换，可能丢掉重复键，不能据此满足 Python 的严格协议。
3. **字节边界。** 大小限制按 UTF-8 字节计算，不按 Rust chars 数量。保留正文原文，不自动修剪或规范化。空白判定应对齐 Python 参考语义；共享用例包含 CJK 文本和控制空白，不能只依赖两种语言各自的 trim 默认行为。
4. **原子提交。** 记忆、输出消息、输入消费和 step 完成状态一起提交。任何非法目标、权限或容量问题都回滚整轮，不静默删除 Action。
5. **预算保守计数。** 请求预留、重试、JSON 修复和未知 usage 都保留；暂停、重启和新建 session 不清零全局预算。显式 reset 保留历史，终止会话不复活。
6. **停止与恢复。** 先持久化停止屏障，再取消模型和结算已有命令；旧结果不能提交。已提交结果不重发，未提交输入只按配置有限恢复。
7. **导出不是权威状态。** SQLite 是权威记忆；文件失败不重跑已提交模型步骤。已开始的写入必须跟踪，直到写入及回执结算后才能释放 Store。
8. **关闭有结果。** 并发 close 共用一次关闭流程，调用者取消不终止清理；异常报告与资源释放分开。异步客户端、SQLite 工作线程与文件回执要显式关闭并等待，不能把完整异步清理寄托在 Drop 上。可参考 [Tokio graceful shutdown](https://tokio.rs/tokio/topics/shutdown)。

已经落地的协议准备材料：[共享 Decision 用例](../spec/decision-vectors.json)、[参考语义](../spec/README.md)。未来 Rust 测试必须消费同一份原始 JSON 字符串，而不是重新构造“看起来一样”的对象。

## 数据库和锁：先隔离，再证明互通

第一期 Rust 使用独立目录，例如 `.mmar-rust/`，schema 与持久化行为以 Python 版为参考，不直接接管用户已有的 `.mmar/*.db`。直到以下验证完成，才允许互相接管关闭后的 Store：

- 表、索引、PRAGMA、schema version、Limits 编码和恢复行为一致。
- Python 请求幂等哈希基于紧凑、排序键、非 ASCII 转义的 JSON；Rust 需要字节级一致的规范编码，不能仅比较解析后的对象相等。
- Python 在 Windows 锁 `.db.lock` 的首字节，Unix 使用 flock；Rust 的标准库锁是否与之互斥必须通过真实双进程测试确认。锁文件内容不是所有权证明，不依赖 PID 字符串或删除锁文件夺取所有权。
- Python 写入 → Rust 读取/恢复 → Python 重新读取均通过；包含中断 attempt、预算耗尽、停止会话和多次恢复上限。
- 未知 schema 明确拒绝；不能为了读取而自动“修复”或覆盖数据库。

即使验证互通，也只有一个后端能拥有同一 Store。性能比较使用各自新的 Store 和相同持久化设置。

## 分阶段交付

| 阶段 | 可运行交付 | 通过条件 |
|---|---|---|
| R0：准备（本轮） | Python 清理回归、协议 JSON 用例、后端契约和路线 | Python 校验用例通过，资源异常诊断不再复现；Rust 状态明确为未实现 |
| R1：协议和 Store | Cargo 工程、严格 Decision 解析、独占锁、事务和 inspect | cargo fmt / clippy / test；共享用例全通过；SQLite 异常和锁测试通过 |
| R2：离线 Runtime | Mock demo、并发、消息、记忆、事件、暂停/停止/恢复 | 多目标、广播、背压、跨会话串行、预算与强制退出场景和 Python 行为一致 |
| R3：真实模型 | 可选 API feature、共享客户端、有限通信测试 | Mock HTTP 故障测试通过后，再以显式预算运行 30 Agent；记录 tokens、修复和延迟 |
| R4：迁移接入 | 配置、inspect、导入 / 导出与部署方式；按需提供宿主桥接 | 已有 CLI 场景可迁移；已有数据库接管必须通过双进程锁与双向恢复测试；桥接如实现需验证版本、背压和退出语义 |
| R5：整体切换 | Rust 默认运行、发布二进制、维护与兼容说明 | 完整行为和恢复验收通过，记录内存、启动时间、本地吞吐与构建成本；Python 核心冻结为参照 |

子进程桥接可采用一行一个 JSON 的命令 / 响应与带 seq 的事件。stdout 只承载协议，stderr 承载脱敏诊断；key 从进程环境读取，不能放进命令帧。命令、事件队列与单帧大小必须有上限；模型 Action 协议与宿主 IPC 协议应分别版本化。

## 整体切换前的资源验收

先测 10 / 100 / 1,000 / 10,000 角色的闲置状态、稀疏激活和持续历史，再测 30 Agent 真实模型。大目录实验需要显式提高容量限制。两端采用相同并发、上下文、SQLite FULL 持久化、调用预算和模型配置，独立进程重复测量。

Rust 可减少解释器和部分对象开销，具体收益仍待实测；语言变化本身不会减少模型 tokens、网络等待或持久化所需的磁盘同步。对真实 LLM 任务，本地加速可能只占总耗时的小部分。整体迁移方向已经确认，验收需要如实量化收益并确保行为一致性，不能仅凭小型 Mock 吞吐跳过恢复与安全边界。
