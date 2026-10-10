# 30 Agent 真实模型验证

日期：2026-10-10。使用用户授权的 API 配额，仅将 key 放入测试进程环境；未写入源码、报告、提示词或 SQLite。

## 接入配置

- 提供方：Intern Discovery；API 根路径 `https://discovery-api.intern-ai.org.cn/v1`。
- 模型：`deepseek-v4-flash-0731`。
- Chat Completions，`max_tokens=512`，`response_format={"type":"json_object"}`，非流式。
- Python 3.11.15；SQLite 3.53.1；同一 HTTPX 客户端和 SQLite 工作线程。

先进行 1 次连通性调用：HTTP 200，JSON 协议有效，约 2.4 秒，提供方报告 366 tokens。

## 测试与结果

30 位逻辑 Agent 组成环。每位收到一次外部输入，由真实模型生成一个 send Action 给下一位；收到同伴消息后，模型更新记忆并保持沉默。所有模型输出经 Runtime 校验，消息和记忆在 SQLite 原子提交。每批输入为 1 条，以分别验证首次发言和接收。此测试验证有限通信流程，不代表开放式长期讨论质量。

| 指标 | 结果 |
|---|---:|
| 注册 / 成功执行的 Agent | 30 / 30 |
| 并发上限 / 观察到的在途模型调用峰值 | 4 / 4 |
| 成功提交 step | 60（每 Agent 2 次） |
| 投递 / 消费 | 60 / 60（外部输入 30，同伴消息 30） |
| 发送到正确下一位的 Agent | 30 |
| 记忆更新成功的 Agent / 更新事件 | 30 / 60 |
| 模型调用 / 调用预算 | 63 / 80 |
| JSON 修复调用 | 3 |
| HTTP 响应 | 63 次 200 |
| 失败 step / 待处理投递 | 0 / 0 |
| 处理完成 / 关闭后的会话状态 | IDLE / STOPPED |
| SQLite integrity_check | ok |
| 耗时，不含注册和连通性调用 | 95.929 秒 |
| 闲置后台 asyncio 任务 | 2 |
| 闲置进程工作集 | 35.82 MiB |
| 每秒采样的进程工作集峰值 | 42.62 MiB |
| 提供方报告输入 / 输出 tokens | 41,459 / 18,352 |
| 提供方报告总 tokens | 59,811 |

3 次模型响应未通过本地 JSON 校验，各经一次修复后成功；不以 HTTP 200 代替协议验证。修复也占用调用预算和 token。不能从本次数据确认首次无效输出的具体原因。输出 usage 可能包含模型推理 token，不等同于可见消息长度。含连通性调用，本次合计 64 次请求、60,177 tokens；未估算金额。

内存为 Windows 进程工作集，包含 Python、SQLite、HTTPX 和 TLS；采样峰值并非严格测得的瞬时最大值。结果是单次本机试跑，受网络和提供方负载影响，不能外推几十并发或长期服务表现。

真实消息示例：`agent000 -> agent001`：“使用共享缓存避免重复计算，减少推理调用次数。”所有 30 条消息及计数见 [JSON 报告](benchmarks/live-intern-30.json)。测试程序见 [benchmarks/live_scale.py](../benchmarks/live_scale.py)，复现命令见 [README](../README.md)。

密钥不包含在可提交文件中。运行生成的数据库和本地报告位于 `.mmar/`，由 `.gitignore` 排除。远端取消后的计费、断网恢复和长时间开放式讨论不在这次实测范围内。
