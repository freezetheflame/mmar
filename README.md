# MMAR MVP

轻量、事件驱动的多 Agent 运行时。Agent 只有身份和持久记忆，共享 Runtime 负责调度与通信。采用 **Python 3.11+ / asyncio / SQLite**；离线核心没有第三方运行依赖，真实模型接口可选 HTTPX。

已经支持：自主选目标、单目标与多目标发送、群组广播、沉默、记忆替换、动态注册注销、多会话、批处理、并发与预算、暂停停止、持久事件、重启恢复和 Markdown 导入导出。

## 先运行离线示例

在项目目录执行：

```powershell
python -m mmar demo --agents 10
```

此命令使用确定性 Mock，展示多目标发送、广播、更新记忆和最终沉默；没有网络调用。真实模型才会按身份、记忆和收到的消息自主决策。每次 demo 创建新的 `.mmar/demo-*.db`，避免混用实验预算。

运行三位具名 Agent 的文件示例：

```powershell
python -m mmar run examples/discussion.toml
```

身份在 `examples/agents/*/agent.md`，记忆导出到各自的 `memory.md`。SQLite 保存权威状态。改动记忆文件后需要暂停并显式 import，文件变动不会直接覆盖在途执行。

查看已保存的消息，不产生推理请求：

```powershell
python -m mmar inspect .mmar/discussion.db
```

## 安装与可复现依赖

已提供 `uv.lock`。可使用 uv 创建隔离环境并安装固定版本：

```powershell
uv sync --frozen --extra api
uv run --no-sync mmar demo --agents 10
```

也支持 pip：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[api]"
```

不需要真实 API 时，可安装 `-e .` 或直接在源码目录执行。核心不安装 Agent Framework、Web 服务、向量库或模型 SDK。

## 后续接入 API key

实现的是 OpenAI-style Chat Completions 接口，base URL 指向提供方 API 的根路径，例如以 `/v1` 结尾；运行时拼接 `/chat/completions`。模型名称由你指定。

PowerShell 7 中设置当前进程环境：

```powershell
$env:MMAR_API_KEY = Read-Host "API key" -MaskInput
$env:MMAR_MODEL = "提供方的模型名称"
$env:MMAR_API_BASE = "https://api.openai.com/v1"
uv run --no-sync mmar demo --api --agents 3 --calls 10 --concurrency 2 --rate 1 --timeout 120
```

`--calls 10` 限制本次 Store 的请求预留次数，失败、重试和 JSON 修复也计入。`--rate 1` 共享限速为最多每秒启动一个请求；`--output-tokens 1024` 限制单次生成。取消请求并不保证提供方没有执行或计费。

部分兼容服务使用旧的 token 参数，或不支持 JSON mode，可以显式调整：

```powershell
$env:MMAR_TOKEN_PARAMETER = "max_tokens"
$env:MMAR_JSON_MODE = "0"
```

关闭 JSON mode 后仍会提示模型返回 JSON，并执行同样的本地校验。不会为了自动探测能力额外发送请求。

key 只读取到模型客户端内存并用于 Authorization header，不写入 SQLite、Agent 上下文或日志。`.env.example` 只是配置说明，程序不会自动读取 `.env`。TLS 验证开启，禁止远程明文 HTTP；不跟随重定向、不自动读取代理环境变量，不执行模型生成的代码或工具。

目前真实提供方尚未端到端验证；本地 HTTPX MockTransport 测试已覆盖请求格式、429、取消和响应大小限制。协议按 [OpenAI 官方请求参考](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create) 与 [HTTPX 异步接口](https://www.python-httpx.org/async/) 核对。JSON 格式仍由 Runtime 本地验证。

## Python 环境接口

```python
import asyncio
from mmar import Runtime, Limits, MockModel

async def main():
    async with await Runtime.open(".mmar/custom.db", model=MockModel(),
                                  limits=Limits(concurrency=2)) as runtime:
        await runtime.register_agent("alice", identity="你是研究员 Alice。")
        await runtime.register_agent("bob", identity="你是工程师 Bob。")
        await runtime.create_session("discussion", ["alice", "bob"])
        await runtime.inject_message("discussion", "alice", "开始讨论", "seed-1")
        await runtime.start()
        print(await runtime.wait_idle("discussion", timeout=30))

asyncio.run(main())
```

`MockModel()` 默认沉默，可以通过同步或异步 `policy(snapshot)` 自定义模拟决策。ModelAdapter 只有 `complete(snapshot, repair=False)` 与 `aclose()`；每次 complete 对应至多一次真实模型请求。

事件消费与消息内容查询：

```python
async for event in runtime.events(after_seq=saved_cursor):
    if event["kind"] == "message.accepted":
        message = await runtime.read_message(event["data"]["message_id"])
        await environment.handle(message)
    saved_cursor = event["seq"]  # 环境根据自己的需求持久保存
```

保存游标前崩溃会再次收到事件；外部副作用按 seq 幂等。慢消费者不阻塞 Agent 提交。停止后迭代器读完已有事件退出。

其他管理接口包括 `activate`、`get_agent_state`、`get_session_state`、`read_memory`、`pause`、`resume`、`stop`、`stop_session`、`unregister_agent`、`set_participants`、`reload_identity`、`import_memory`、`flush_exports`、`reset_budget`。

## 执行与资源边界

- 无输入的 Agent 不创建 Task、Queue、Lock 或模型客户端；身份、记忆和投递放在 SQLite。
- 常驻 asyncio 任务为共享 scheduler 和 exporter，另有一个 SQLite 工作线程；文件导出使用共享线程池。
- 创建 step Task 前取得执行机会，最多 `concurrency` 个在途 step。同 Agent 跨 session 串行。
- 一个 step 的记忆、输出消息、输入消费与完成事件整体提交；非法 Action 或一个满收件箱导致整轮失败。
- 模型 Action 只允许 send 和 broadcast；空 actions 表示沉默。Agent 不能执行工具、修改身份或自行醒来。
- 广播按实际 Delivery 数计数，默认不回送发送者；目录完整但受上下文字节预算约束。
- 同一 Agent 的记忆跨 session 共享。需要隔离时使用不同 Agent ID。
- stop 先落持久屏障，之后的旧结果不能提交；drain 仅等当前 step，不继续执行整条消息链。
- 崩溃后已提交结果不重发；未提交输入最多按配置恢复一次。次数和未知 usage 保留，不自动退款。
- 暂停允许在途 step 完成，并接受受容量限制的输入；暂停、重启和新建 session 不清零 Store 总预算。
- 同一 Store 仅一个 Runtime 实例拥有；禁止复用已退役 Agent ID。

主要默认限制：并发 4、每批 8 条输入、每 step 4 个 Action / 64 次投递、每 Agent 128 条未完成输入、全局 4,096 条未完成输入；每条正文 8 KiB、记忆 8 KiB。完整默认值见 `mmar/protocol.py`。

Limits 配置随 Store 持久化。重新打开时必须使用相同 Limits，避免不经意重启增加额度。新配置使用新 Store；明确重开实验预算时，在 CREATED 或 PAUSED 状态调用 reset_budget，保留旧全局计数历史。已终止 session 不复活，应创建新 session。

`metrics()` 报告注册数量、在途执行、未完成投递、各状态 step、请求和投递计数、事件游标与已报告 token 用量。缺失 usage 的 attempt 单独计数；“已报告 token 合计”不代表精确总费用。

## 测试与资源测量

```powershell
uv run --no-sync python -m unittest discover -v
uv run --no-sync python -m benchmarks.mock_scale --output docs/benchmarks/mock-scale.json
```

测试包含真实子进程强制退出，覆盖 claim 后、commit 期间和 commit 后崩溃；测试不会调用真实模型。资源报告见 [Mock 测量结果](docs/benchmarks/mock-scale.json)，测量规模为 10、100、1,000 个注册 Agent，模型并发固定为 4。

报告分别列 Python 追踪分配与 Windows 进程工作集；追踪分配不包含解释器和 SQLite 原生内存。Mock 吞吐不是实际 LLM 吞吐，上下文目录和磁盘提交会影响测量。

当前 MVP 保留执行和文本版本历史，尚未自动归档或清理；预算重置和持续注册会增加磁盘使用。旧实验可在关闭 Runtime 后归档整个 Store。尚无 HTTP/WebSocket 管理服务、图形界面或提供方专属 token 计量器；通过 Python API 接入环境。

架构基线见 [v0.4 设计](docs/mmar-v0.4-design.md)。
