# 资源清理修复与验证

日期：2026-10-11。本轮处理 Runtime / Store 的初始化、关闭与导出生命周期；不更改数据库 schema、持久化等级或调用预算。测试与诊断使用 Mock，真实提供方请求为 0。

## 修复后的行为

- `Runtime.open` 接管传入的 Adapter，包括路径处理、Store 构造、工作线程启动、数据库初始化失败，以及调用者取消的路径。失败后释放已获得的 Store 所有权，并关闭 Adapter；清理异常以类型注释附加到原始初始化错误。
- Store 获得独占锁后，如果工作线程无法启动，立即释放锁。
- 并发 `close()` 共用一次关闭任务；取消某个等待调用者不会取消清理。关闭开始后拒绝新的写入与 start / resume。
- 导出异常不再跳过 Adapter 和 Store 清理。已经开始的文件写入通过共享批次跟踪，写入和数据库回执结算后才关闭 Store；空批次不增加闲置后台任务。
- 单项清理失败报告原始异常，多项失败报告 `BaseExceptionGroup`。完成清理后的重复 close 返回同一结果，包括再次报告已发生的异常，不重复关闭 Adapter。

## 期限与所有权

`await runtime.close(timeout=10)` 使用正数、有限秒数；并发关闭时采用第一个调用者的期限。期限用于停止模型任务、刷新导出和等待 Adapter 关闭各阶段，**不是整个关闭流程的绝对墙钟上限**。已经接纳的 SQLite 命令和阻塞文件写入需要结算，避免释放数据库后仍有回执写入。

自定义 Adapter 的 `complete()`、`aclose()` 必须配合 asyncio 取消。同一个 Adapter 不应交给多个 Runtime。如果在途模型忽略取消，close 报 `stop_timeout` 并保留 Store 所有权，待模型结束后可以重试 close。若 Adapter 的 aclose 超时，则请求取消它、报告 `model_close_timeout` 并继续释放本地 Store；忽略取消的外部 Adapter 不能被 Runtime 安全强杀。

`close()` 抛出异常可能发生在本地资源已经释放之后，不能仅根据异常认定数据库仍被占用。建议在常规程序中使用 `async with await Runtime.open(...)`，由上下文退出负责关闭，并让宿主记录关闭异常。

## 验证结果

| 验证 | 结果 |
|---|---|
| Python 3.11.15 完整回归 | 62 项通过 |
| Python 3.12.2 完整回归 | 62 项通过 |
| 新增生命周期回归 | 14 项，覆盖初始化失败、锁释放、取消、并发 / 重复关闭、导出失败、Adapter 超时、在途文件写入与停止超时 |
| 跨语言协议用例 | 27 个原始 JSON 输入，由 1 项参数化测试验证 |
| 闲置任务回归 | 仍为 2 个后台 asyncio 任务 |
| 导出异常后的泄漏诊断 | runtime 已关闭，Store 线程已退出，不再复现 |
| 初始化拒绝后的 Adapter 泄漏诊断 | Adapter 已关闭，不再复现 |

回归入口：[生命周期测试](../tests/test_lifecycle.py)、[协议一致性测试](../tests/test_conformance.py)。诊断原始结果：[本轮快照](benchmarks/review-checks-cleanup.json)；对照：[修复前快照](benchmarks/review-checks-v0.1.json)。

```bash
python -m unittest discover -q
python -m benchmarks.review_checks --output docs/benchmarks/review-checks-cleanup.json
```

限速等待占用 request_timeout 的问题仍可复现，本轮未修改其语义；下一项优化应分开准入等待与实际请求期限。Rust 后端的接口、依赖候选、共享语义与分阶段验收见 [Rust 模式路线](rust-mode-plan.md)，当前尚未实现 Rust 可执行版本。
