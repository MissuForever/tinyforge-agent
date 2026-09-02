# TinyForge 论文驱动改进

## GenericAgent：论文主张

参考论文为 [GenericAgent: A Token-Efficient Self-Evolving LLM Agent via Contextual
Information Density Maximization](https://arxiv.org/abs/2604.17091)（arXiv:2604.17091）。它把长任务 Agent
的核心问题定义为“上下文信息密度”，强调同时保持决策信息的完整性和简洁性。

论文提出四项机制：最小原子工具、分层按需记忆、经验证的经验固化、分层上下文压缩。
其中最强的组件级证据来自 Table 5：165-token 的 Condensed Memory 与 288-token 的冗余
记忆取得相同 66.48% TSR，而 575-token Full Memory 只有 52.44%。这支持“默认只展示高密度
索引、深层知识按需读取”，但实验仅覆盖一个子集，不能证明所有任务都不需要向量检索。

## GenericAgent：TinyForge 实现映射

| 论文机制 | TinyForge 实现 | 关键约束 |
| --- | --- | --- |
| Minimal atomic tools | 6 个工作区工具，按配置增加 3 个记忆工具和 3 个 Skill 工具 | Skill 只负责发现和读取，不注册任务专用执行器 |
| Working memory | 每轮动态 system anchor | 目标、约束、进度、关键事实、下一步、20 条摘要 |
| L1 index | `index.json` 元数据指针 | 最多注入 24 条且不超过 2200 字符 |
| L2 facts | `facts/*.json` | 只保存稳定、可复用、带成功证据的事实 |
| L3 SOPs | `sops/*.json` | 必须有最后一次风险操作之后的测试/检查证据 |
| L4 archive | `sessions/*.json` | 外置、最多 200 条/60 KB、凭据脱敏、默认不注入 |
| No Execution, No Memory | `e1` 等运行时证据 ID | 未执行、执行失败或未知 ID 均拒绝暂存 |
| Triggered commit | `stage_memory` + 明确完成状态 | 收尾重新验证证据；失败时不晋升长期记忆 |
| Layered compression | 旧输出压缩 + 完整批次 FIFO | 压至 60% 水位，保留最新工具批次与 anchor |
| Progressive Skill disclosure | `list_skills` -> `load_skill` -> `read_skill_resource` | 默认关闭、显式授权，正文不进入 system message |
| Efficiency evaluation | `AgentResult` 与 CLI `[stats]` | 请求、工具、输入/输出/cache Token、耗时 |

持久状态默认位于 `~/.tinyforge/workspaces/<sha256(workspace)>`，而不是目标仓库中。这样既
避免 Git 污染，也避免普通文件工具直接改写记忆。检索使用标题、关键词和正文的轻量词法
评分，不需要额外 embedding 模型；L1 只告诉模型“哪些知识存在”，模型决定何时调用工具。

Skill 与论文中的自演化 Memory 分开管理。`SkillCatalog` 读取用户和仓库预先编写的静态说明，
`SkillRuntime` 只有在真实进程环境、CLI `--skills` 或 GUI `Skills` 明确授权后才向模型开放工具。
模型先看有界元数据，再加载一个相关正文，最后只在需要时读取被引用资源。所有来源内容都
作为带 `untrusted: true` 的 tool output 返回，永不提升为 system 指令。相反，Memory 只能由
运行时成功证据产生；三个 Skill 工具被证据记录器排除，加载说明不能满足
No Execution, No Memory。

## GenericAgent：工程补全

论文没有规定文件 Schema、并发事务、冲突过期、验证判据或凭据处理。TinyForge 因此做出
以下可审计选择：

- 索引和条目使用 JSON，写入采用同目录临时文件后原子替换；
- 同一工作区内同类同标题记忆视为修订，保留 revision；
- 证据来自结构化工具结果，而不是模型自述；
- `run_command` 非零退出码不是成功证据；
- SOP 必须由最后一次文件修改、失败命令或未验证命令之后的可识别测试/检查命令验证；
- 归档限制每条消息和总长度，并过滤常见 API Key/Token 形式；
- Skill 使用 `user:<name>`/`workspace:<name>` 稳定 ID，严格解析 frontmatter，拒绝链接、越界和
  超限资源；工作区 `.env` 不能开启 Skills 或重定向用户 Skill 根；
- Skill 默认关闭，CLI `/skills` 与 GUI Skills 页签只展示有界概览，启用后模型才获得三级读取工具；
- CJK 按约 1.25 token/字符保守估算，ASCII 按约 4 字符/token，避免论文固定比例对中文延迟裁剪。

这些规则能降低记忆污染，但不能证明记忆永远正确。标题相同的错误修订、环境变化导致的
SOP 过期、命令成功但语义验证不足仍是剩余风险。`run_command` 已从继承环境中删除
`OPENAI_API_KEY` 和 `TINYFORGE_API_KEY`，但仍复制其余 `os.environ`；其他 Token、云凭据或
自定义 Provider 密钥因而可能进入子进程。`.env` 的独立解析不会主动污染环境，但仍应把
子进程环境白名单和外部沙箱作为执行不可信代码前的必要工程补全。

## SkillAdaptor：论文主张与证据

第二篇参考论文为 [SkillAdaptor: Self-Adapting Skills for LLM Agents from
Trajectories](https://arxiv.org/abs/2606.01311)（arXiv:2606.01311）。它把失败后的 Skill
维护拆为三步：Localizer 定位最早可行动故障，Linker 只在本次检索到的 Skill 中估计责任并
选择 `REVISE` 或 `GENERATE`，Qualifier 对原集合 `K` 和候选集合 `K+` 重新执行任务，只有
候选指标不下降时才接受更新。论文使用 Qwen3-Embedding-8B 做阈值 0.45、Top-10 检索，再由
主模型重排；新 Skill 相似度超过 0.95 时拒绝。

最关键的组件证据来自 Table 3。Kimi-K2.5 上，完整版 WebShop Score/Succ 为
`41.6/33.0%`；去掉 Localizer 和 Linker 后降到 `35.8/28.6%`，去掉 Qualifier 后降到
`34.0/26.3%`。去掉 Qualifier 时 PinchBench spread 也从 `±5.2` 增至 `±8.1`。这支持“精确
归因和验收门比单纯积累 Skill 更重要”，但消融只覆盖一个骨干模型，不能推出任意模型或任意
项目都会受益。论文还明确指出，Skill 增加输入 Token；交互步数减少不等于总计算成本更低。

证据本身也有边界：论文称代码“将发布”；长期部署和分布漂移未验证；Research、Memory、
Security 等依赖外部状态的任务收益较弱；Table 2 与 Table 3 对 Kimi WebShop Succ spread
分别写成 `±11.1` 和 `±1.0`，存在内部不一致。因此 TinyForge 不把论文中的高温生成和在线
改写直接移植到用户工作区。

## SkillAdaptor：TinyForge 映射

| 论文机制 | TinyForge 当前实现 | 证据边界 |
| --- | --- | --- |
| 检索后重排 | `list_skills` 省略 query 时按当前任务做零依赖词法 Top-K，显式空 query 才浏览全部；主模型从候选中选择 | 这是词法近似，不是 Qwen embedding 或论文的余弦阈值 |
| Skill provenance | 每次加载保存 `SKILL.md` SHA-256、资源 manifest、加载步骤和资源读取记录 | receipt 证明版本何时生效，不证明内容正确 |
| 资源一致性 | 加载时冻结可读资源的文件签名和内容 SHA-256；新增、替换或变化的资源在本任务后续读取时 fail closed | 新会话重新发现后可接受用户有意更新 |
| Step-level trajectory | 有界记录最多 200 个工具名、结果摘要和生成该工具批次时上下文中可见的 Skill | 不记录参数或完整思维链，也不把原始大输出复制到报告 |
| Localizer 基础 | 失败任务报告第一个可观察的非 Skill 工具失败步骤 | 这是待审查候选，不宣称等于语义根因 |
| Linker 基础 | 只列出决策时仍有输出保留在模型上下文中的 digest receipt；同批加载和已压缩移除项不算 active | `attribution_status=unresolved`，不自动分配责任权重 |
| Qualification gate | 报告固定为 `qualification_status=not_run`、`skill_mutation_applied=false` | 没有隔离 A/B 重跑就不能接受候选 |
| 可观测性 | CLI 输出只读 Skill review；GUI 展示 Catalog、Searches、Loaded、Resource reads 和 Adaptation 报告 | GUI 没有 Apply 按钮，不绕过授权边界 |

命令侧也吸收了论文附录的安全约束：默认策略按实际命令段识别 `sudo`、`su` 和系统关键目录
破坏性写入，不因 `echo`、`Write-Output` 或提交消息中的自然语言提及而误报。该解析仍只是
降低误操作风险的策略护栏，不是完整 shell 解释器或系统沙箱；`run_command` 也仍可能继承
除两个 Agent API Key 之外的其他宿主凭据。

## 自动适配的资格门

当前版本不生成、写入或执行候选 Skill。要进一步实现论文的 `REVISE/GENERATE`，至少需要：

1. 候选存入工作区外的隔离区，并绑定原 Skill digest，过期候选 fail closed；
2. 生成集与资格集分离，不能用同一个失败样本同时生成并作为唯一验收；
3. 在无宿主凭据、`allow_dangerous=false` 的隔离副本中重跑 targeted check 和既有回归；
4. 目标行为必须从失败变为通过，回归不得变差；仅 `TASK_COMPLETE` 或 `Δ=0` 不足以验收；
5. 最终 diff 经用户明确确认后才可原子写入活动 Skill。

在这些条件完成前，项目可以准确称为“带 step-level Skill attribution audit 的 Coding Agent”，
不能称为论文完整的自适应 SkillAdaptor。

## 暂缓机制

暂未实现浏览器工具、Reflect/watchdog、自治探索、subagent 管理和自动生成、自动执行或自动
提交 Skill。当前 Skill 仍是显式授权、只读加载的本地说明；新增 trace 和 fault report 只提供
版本化审计依据，不会注册代码或执行 `scripts/`。对当前编程 Agent，先证明工作记忆、受验证
长期记忆、渐进披露、逐步归因和上下文压缩更有价值。

## 可复现实验

可用相同任务对比无记忆冷启动和记忆热启动：

```powershell
py -3 -m tinyforge --no-memory -w .demo/order_total "修复实现并运行全部测试"
py -3 -m tinyforge -w .demo/order_total "修复实现并运行全部测试"
py -3 -m tinyforge -w .demo/order_total "检查并验证订单计价实现"
```

记录每次 CLI 的 `[stats]`：请求数、工具调用数、输入/输出 Token 和耗时，同时必须记录最终
测试结果。只有质量不下降且重复任务成本降低，才能声称记忆产生了论文所讨论的效率收益。

Skill 也应单独做关闭/开启对照，避免把预置操作指南的收益误归因于 Memory：

```powershell
py -3 -m tinyforge --no-skills -w .demo/order_total "检查并验证订单计价实现"
py -3 -m tinyforge --skills -w .demo/order_total "检查并验证订单计价实现"
```

除最终测试外，还应记录模型是否调用 `list_skills`、实际加载的稳定 ID、资源读取次数和新增
Token。只有 Skill 匹配任务且质量或成本改善，才能说明三级披露提供了有效的信息密度提升。
