# MMAR v0.2 — 第一轮：让 Agent 只保留定义

日期：2026-10-10。输入：用户提供的 v0.1 Draft。本稿是评审中间版本，不作为实现规格。

## 目标与范围

保留原设计的通信自主性、持久记忆、群组、动态注册、并发、预算、事件和环境接口；减少每个 Agent 的对象、常驻任务与基础设施。此次工作只评审架构，不实现 Runtime。

## 候选设计

```text
AgentSpec = ID + identity_ref
AgentMemory = 由共享 Store 保存的文本
DecisionEngine.decide(snapshot) -> Decision
Runtime = registry + scheduler + route + validate + commit
```

1. Agent 不拥有模型客户端、收件队列、asyncio Task、Lock、订阅器或后台线程。
2. 执行器是共享函数，不需要 Agent 基类或继承层次。
3. 只给正在执行的 Agent 创建任务；注册量与推理并发分离。
4. Message Bus 和 Scheduler 先作为 Runtime 内部职责，不建立独立服务或公共插件协议。
5. `actions=[]` 表示沉默，删除冗余的 `noop` Action。
6. 记忆更新保留整段替换，简化为可空的 `memory_replace` 文本。
7. Runtime 构造消息的 sender、ID、会话和时间，模型只输出意图。
8. 外部显式 activate 转换为持久的输入事件，复用同一调度路径。
9. 共享模型适配器只处理请求与响应，不处理身份、记忆或路由。

## 保留的问题

| 问题 | 为什么仍不能定稿 |
|---|---|
| Markdown 是记忆真源，SQLite 是消息真源 | 写记忆成功、消息事务失败时，一次 step 只完成一半 |
| Agent 可以看到哪些其他 Agent | 没有通信目录，自主选目标缺少明确输入 |
| 多会话输入合并 | 输出的归属和预算来源不明确 |
| 同一个 Agent 的待执行标志 | 调度和新消息竞争可能丢失唤醒 |
| 广播只限制动作数 | 一个动作可以展开成大量实际投递 |
| stop 只取消 Task | 取消不等于服务端停止，也不自动阻止迟到结果提交 |
| 事件回调在提交路径里执行 | 慢回调或异常可能拖住执行或误导重试 |
| 消息 ID 去重 | 多目标需要独立投递身份；输入消费还缺少状态 |

## 第一轮结论

Agent 可以进一步缩小为定义和外置记忆；无需额外的 Agent 服务层。这个方向成立，但仅把组件搬到 Runtime 还不能形成可靠的执行协议。下一轮需要统一提交与恢复规则。
