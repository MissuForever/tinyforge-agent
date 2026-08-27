# GenericAgent 论文驱动改进

## 论文主张

参考论文为 [GenericAgent: A Token-Efficient Self-Evolving LLM Agent via Contextual
Information Density Maximization](https://arxiv.org/abs/2604.17091)（arXiv:2604.17091）。它把长任务 Agent
的核心问题定义为“上下文信息密度”，强调同时保持决策信息的完整性和简洁性。

论文提出四项机制：最小原子工具、分层按需记忆、经验证的经验固化、分层上下文压缩。
其中最强的组件级证据来自 Table 5：165-token 的 Condensed Memory 与 288-token 的冗余
记忆取得相同 66.48% TSR，而 575-token Full Memory 只有 52.44%。这支持“默认只展示高密度
索引、深层知识按需读取”，但实验仅覆盖一个子集，不能证明所有任务都不需要向量检索。

## TinyForge 实现映射

| 论文机制 | TinyForge 实现 | 关键约束 |
| --- | --- | --- |
| Minimal atomic tools | 6 个工作区工具 + 3 个记忆工具 | 不为具体任务增加专用工具 |
| Working memory | 每轮动态 system anchor | 目标、约束、进度、关键事实、下一步、20 条摘要 |
| L1 index | `index.json` 元数据指针 | 最多注入 24 条且不超过 2200 字符 |
| L2 facts | `facts/*.json` | 只保存稳定、可复用、带成功证据的事实 |
| L3 SOPs | `sops/*.json` | 必须有最后一次风险操作之后的测试/检查证据 |
| L4 archive | `sessions/*.json` | 外置、最多 200 条/60 KB、凭据脱敏、默认不注入 |
| No Execution, No Memory | `e1` 等运行时证据 ID | 未执行、执行失败或未知 ID 均拒绝暂存 |
| Triggered commit | `stage_memory` + 明确完成状态 | 收尾重新验证证据；失败时不晋升长期记忆 |
| Layered compression | 旧输出压缩 + 完整批次 FIFO | 压至 60% 水位，保留最新工具批次与 anchor |
| Efficiency evaluation | `AgentResult` 与 CLI `[stats]` | 请求、工具、输入/输出/cache Token、耗时 |

持久状态默认位于 `~/.tinyforge/workspaces/<sha256(workspace)>`，而不是目标仓库中。这样既
避免 Git 污染，也避免普通文件工具直接改写记忆。检索使用标题、关键词和正文的轻量词法
评分，不需要额外 embedding 模型；L1 只告诉模型“哪些知识存在”，模型决定何时调用工具。

## 对论文的工程补全

论文没有规定文件 Schema、并发事务、冲突过期、验证判据或凭据处理。TinyForge 因此做出
以下可审计选择：

- 索引和条目使用 JSON，写入采用同目录临时文件后原子替换；
- 同一工作区内同类同标题记忆视为修订，保留 revision；
- 证据来自结构化工具结果，而不是模型自述；
- `run_command` 非零退出码不是成功证据；
- SOP 必须由最后一次文件修改、失败命令或未验证命令之后的可识别测试/检查命令验证；
- 归档限制每条消息和总长度，并过滤常见 API Key/Token 形式；
- CJK 按约 1.25 token/字符保守估算，ASCII 按约 4 字符/token，避免论文固定比例对中文延迟裁剪。

这些规则能降低记忆污染，但不能证明记忆永远正确。标题相同的错误修订、环境变化导致的
SOP 过期、命令成功但语义验证不足仍是剩余风险。

## 暂缓机制

暂未实现浏览器工具、Reflect/watchdog、自治探索、subagent 管理和自动生成可执行技能。
原因是它们显著扩大权限与演示面，且论文自己承认自治权重适配、日志和技能树维护尚未充分
验证。对当前编程 Agent，先证明工作记忆、受验证长期记忆和上下文压缩更有价值。

## 可复现实验

可用相同任务对比无记忆冷启动和记忆热启动：

```powershell
py -3 -m tinyforge --no-memory -w .demo/order_total "修复实现并运行全部测试"
py -3 -m tinyforge -w .demo/order_total "修复实现并运行全部测试"
py -3 -m tinyforge -w .demo/order_total "检查并验证订单计价实现"
```

记录每次 CLI 的 `[stats]`：请求数、工具调用数、输入/输出 Token 和耗时，同时必须记录最终
测试结果。只有质量不下降且重复任务成本降低，才能声称记忆产生了论文所讨论的效率收益。
