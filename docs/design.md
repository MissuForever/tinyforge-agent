# TinyForge 设计说明

## 1. 目标与取舍

TinyForge 的目标是实现一个结构完整、容易审计和现场解释的编程智能体。项目选择 Python
标准库和 OpenAI 兼容协议，没有引入 Agent SDK。这样既能清楚呈现模型调用与工具循环，
也避免框架隐式管理消息、重试或执行环境，导致无法解释实际运行过程。

CLI 仍是最小且可组合的主入口。PySide6 / Qt Widgets GUI 是同一 Runtime 上的可选观察层，
用于展示可恢复历史会话、执行时间线、工具证据、代码 Diff、记忆与 Skill 状态、只读命令输出和工作区文件总览；
它不复制 Agent 循环，也不把现成 Agent 封装成界面。两种前端都通过
`runtime.build_agent()` 装配相同模型、工具、记忆和 Skill Runtime。
PySide6 放在 `gui` 可选依赖组，核心 Runtime 和 CLI 因此仍保持零第三方运行时依赖。

## 2. 一轮消息如何运转

1. `Agent` 保存 system、user、assistant、tool 四类消息。
2. `OpenAICompatibleClient` 把历史和工具 JSON Schema 发给模型：固定提供 6 个工作区工具，
   启用记忆时增加 3 个记忆工具，显式启用 Skills 时再增加 3 个 Skill 工具。
3. 若模型返回普通 assistant 文本且没有 tool call，必须用 `TASK_COMPLETE:` 或
   `TASK_BLOCKED:` 明确任务状态，标记会在展示前移除；漏写时 Runtime 只纠正重试一次。
4. 若模型返回 tool call，`CompositeTools` 将其分发给 `WorkspaceTools`、`MemoryRuntime` 或
   `SkillRuntime` 解析参数并执行。
5. `run_command` 执行期间额外发送 stdout/stderr 进度事件供 GUI 展示；成功结果或错误仍会
   编码成 JSON，使用相同的 `tool_call_id` 回填历史。
6. Agent 携带新历史再次调用模型，使模型能够观察结果并决定下一步。

内部历史统一使用 Chat 风格，协议适配器在 HTTP 边界转换：Chat Completions 使用嵌套的
`function` 工具定义和 `tool` 消息；Responses API 使用扁平工具定义，并把调用与结果转换为
共享 `call_id` 的 `function_call` 和 `function_call_output`。这样 Agent 循环不依赖厂商协议。

每个新会话生成稳定 ID，后续轮次原子更新同一份脱敏 JSON，而不是创建重复快照。GUI 和
CLI 都能列出、恢复、重命名和删除会话。恢复只接受未截断且工作区身份匹配的档案，忽略
持久化的 system 消息并按当前配置重新生成系统提示，避免旧提示或手工修改扩大权限。

工具错误不会抛出到顶层，因为“文件不存在”“替换文本不唯一”“测试失败”都是模型应该
观察和处理的工作状态。模型 API 无法访问或协议损坏则属于运行时错误，会由 CLI 明确报告。

## 3. 为什么分别提供 write_file 和 edit_file

`write_file` 适合新文件，但覆盖已有文件会丢失未读内容。`edit_file` 要求模型提交原文和
替换文本，默认还要求原文只出现一次。若文件已被用户或其他步骤改变，替换会失败并要求
重新读取，而不是静默修改错误位置。这是一种轻量的乐观并发检查。

## 4. 终止条件

正常终止是模型返回不含 tool call 且带 `TASK_COMPLETE:` 的最终回答；`TASK_BLOCKED:`
会保留阻塞说明并以失败状态退出。异常终止还有三种：

- 达到 `max_rounds`，避免模型持续消耗时间和额度；
- 经一次纠正后最终回答仍缺少明确状态标记，避免把“无法完成”误判为成功；
- 完全相同的一组工具调用连续出现三次，判定模型进入无进展循环。

命令还有独立超时，避免测试、开发服务器或等待输入的进程永久阻塞 Agent。

## 5. 上下文管理

每项工具输出先限制长度。运行时使用 ASCII/非 ASCII 加权 Token 估算，并把工具 Schema
计入预算，避免中文任务被固定字符比例低估。超限时先压缩旧工具输出，再按“assistant
tool call + 对应 tool result”的完整批次删除到预算的 60%，绝不留下孤立 tool 消息。
系统提示、当前任务、最新完整工具批次和 Working Memory 锚点会被保留。

Skill 正文和资源始终作为普通 `tool` 结果进入历史，不会拼进 system message。旧 Skill 输出
可能随其他工具结果一起压缩或淘汰；模型后续仍需要时应再次调用 `load_skill`，而不是把全部
Skill 长期驻留在上下文中。

## 6. 本地 Skill 与渐进披露

`SkillCatalog` 从固定用户目录 `~/.tinyforge/skills/*/SKILL.md` 和工作区目录
`.tinyforge/skills/*/SKILL.md` 创建有界、只读快照。用户 Skill 根只能由真实进程环境
`TINYFORGE_SKILLS_DIR`、CLI `--skills-dir` 或显式配置覆盖，不能由工作区 `.env` 重定向。
每个 `SKILL.md` 只接受严格的 `name`、`description` 单行 frontmatter 和 UTF-8 正文；目录链接、
Windows reparse point、非法名称、过大文件和越界资源会被跳过。`user:<name>` 与
`workspace:<name>` 是稳定 ID，同名时必须指定 scope，避免静默覆盖。

Skills 默认关闭。只有通过真实进程环境 `TINYFORGE_SKILLS_ENABLED=true`、CLI `--skills` 或
GUI 的 `Skills` 复选框显式授权，当前配置快照才会把 `SkillRuntime` 加入 `CompositeTools`。
Catalog 可以为 CLI `/skills` 和 GUI 的只读 Skills 页签提供有界元数据概览，但关闭时模型
看不到 Skill 工具、元数据或正文。启用后的披露分三层：

1. `list_skills` 只返回名称、描述、scope、稳定 ID 和词法 relevance；省略 query 时用当前任务
   做有界 Top-10 推荐，显式空 query 才按目录浏览；
2. `load_skill` 只加载一个匹配任务的正文，记录正文 SHA-256、加载步骤和资源 manifest；
3. `read_skill_resource` 仅在成功加载后分页读取 `references/` 或 `scripts/` 中加载时已存在且
   文件签名与内容 SHA-256 均未变化的文本。

三种结果都通过 tool message 返回并标记 `untrusted: true`。system prompt 只包含程序内固定的
安全说明，不包含目录名、description、正文或解析错误。Skill 是本地操作指南，不会注册新工具、
自动执行脚本、扩大工作区或授予额外权限；`assets/` 只会出现在清单中，不通过文本读取工具
加载。成功检索、加载和资源读取分别产生不含正文的审计事件，CLI 显示加载 ID，GUI 以
Catalog、Searches、Loaded、Resource reads 展示调用链。Runtime 还为本任务有界记录工具名、
结果摘要和生成该批工具时模型上下文中可见的 Skill receipt；失败收尾时只读报告第一个可观察
的非 Skill 工具失败。同批刚加载但模型尚未读到的 Skill，以及已被上下文压缩移除的 Skill，
不会列入该步骤的候选。报告固定为未归因、未验收、未修改，不会把启发式候选伪装成根因或
自动写回 Skill。
`list_skills`、`load_skill`、`read_skill_resource` 被 Working Memory 明确忽略，因而“读到
一条说明”不能伪装成执行证据，也不能单独支撑长期事实或 SOP。

## 7. 分层记忆与经验固化

Working Memory 每轮注入目标、约束、进度、关键事实、下一步、最近 20 条工具摘要和最近
验证证据。持久层只默认注入有界 L1 指针，L2 事实和 L3 SOP 通过 `recall_memory` 按需读取，
L4 仅用于本地审计，每份档案最多保留最近 200 条消息且 UTF-8 JSON 不超过 60 KB。
状态目录按工作区绝对路径哈希隔离，索引更新使用跨进程文件锁，并通过同目录临时文件
替换完成原子写。

`stage_memory` 必须引用本任务的成功工具证据。SOP 必须引用可识别的测试或静态检查命令；
候选在任务收尾时还会重新校验，确保该验证晚于最后一次文件编辑、失败命令或未验证命令。
只有带 `TASK_COMPLETE:` 的任务才会提交候选；失败任务仍可归档到 L4，但不会污染 L2/L3。
这比单纯相信模型的“已经完成”更严格，也落实了 No Execution, No Memory。

## 8. 安全模型

文件操作使用 `Path.resolve()` 后检查目标仍位于工作区内，因此 `..` 和已存在符号链接都
不能逃逸。命令执行固定工作目录并设置超时，还会按实际命令段拦截常见破坏性命令、
`sudo`/`su` 提权 token 和系统关键目录写删；quoted output、`echo` 与提交消息里的自然语言
不会被当成可执行命令。Windows 命令先
挂起创建，加入 Job Object 后再恢复；无法加入时拒绝执行，超时则终止整棵进程树。
POSIX 命令使用独立进程组。规则检测可以被复杂 shell 表达式绕过，因此
项目明确把它定义为误操作防护，而不是恶意代码沙箱。真正处理不可信代码时，应把整个
进程放进容器、虚拟机或低权限账户。

API Key 只从进程环境或未入库的 `.env` 读取；`.env` 解析到独立映射，不会因解析本身写入
全局环境。`run_command` 会从继承环境中明确删除 `OPENAI_API_KEY` 和 `TINYFORGE_API_KEY`，
但当前仍以 `os.environ` 副本为基础，其他 Token、云凭据或自定义 Provider 密钥仍可能进入命令
子进程。这是尚未消除的风险：运行不可信仓库前应使用最小化启动环境或外部沙箱，后续还应
为子进程改用显式环境白名单。CLI 会脱敏工具参数并截断大段文件内容；
L1-L4 的标题、正文、关键词、证据与嵌套工具参数也会统一脱敏和限长。不过模型仍可读取
工作区内普通文本，因此工作区本身不应放置凭据。

## 9. 可测试性

Agent 只依赖 `ModelClient` 和 `ToolProvider` 两个小协议。测试使用脚本化假模型，无需网络
即可验证 tool call 是否执行、结果是否正确回传及循环是否终止。HTTP 层通过替换
`urlopen` 验证请求和解析，文件及命令工具则在临时目录中执行真实操作。

GUI 线程只负责 Qt 控件渲染，`Agent.run()` 在单独的 daemon 线程运行。同步 `AgentEvent`
先经过有界脱敏，再携带唯一 `run_id` 进入线程安全队列；GUI 线程由 `QTimer` 定时批量消费，
并丢弃旧运行的迟到事件。文件写入前后只读取工作区内、非敏感且不超过 300 KB 的 UTF-8
文本快照，生成用于审计的 unified diff。这样耗时的网络与命令操作不阻塞 Qt 事件循环，
同时所有控件变更仍发生在 GUI 线程。

Skill 测试使用临时用户目录和工作区，覆盖严格 frontmatter、scope 同名、任务感知 Top-K、
三级披露、正文/资源 receipt、加载后资源替换、逐步失败报告、输出上限、资源分页、符号链接/
reparse point、秘密脱敏和 workspace `.env` 不能开启或重定向 Skills。集成测试还断言关闭时
模型工具集合不含 Skill、Skill 内容不进入 system message、成功加载产生事件且不会形成 Memory
evidence。GUI 的 Skills 复选框、结构化只读页和未归因/未验收/未修改状态同样可离屏验证。

中间下方的只读 `Command output` 面板逐行标记 command/stdout/stderr，并把退出码作为独立系统状态展示，
避免工具输出伪造成功状态。ANSI、控制字符、双向文本控制符和凭据会在显示前清理或脱敏；
跨事件片段先聚合成有界行，超长单行直接省略，总历史也有硬上限。命令结束时提供给模型
和记忆模块的仍是原有结构化 JSON，GUI 进度事件不会改变证据判定。

最左侧的 `Workspace` 面板使用独立的只读索引，不把 Agent 的递归 `list_files` 直接暴露给界面。Git
工作区通过 `git ls-files --cached --others --exclude-standard` 复用完整 ignore 语义，再用
porcelain 状态标记修改、添加和未跟踪文件；非 Git 工作区回退为条目数受限的本地扫描。
索引和预览均在后台线程运行，并用工作区代次丢弃切换目录后的迟到结果。目录链接不递归，
预览重新校验真实路径、拒绝敏感路径和链接，并限制字节、字符和行数；UTF-8 文本显示前还会
清理控制字符、双向控制符和常见凭据。文件写入、编辑、命令结束和任务结束会合并触发刷新。

Qt 6 默认使用设备无关像素并跟随操作系统的逐显示器高 DPI 缩放，应用不设置旧版兼容开关
或强制缩放比例。大部分 GUI 支撑逻辑无需显示器即可测试；安装 `.[gui]` 后，还可以设置
`QT_QPA_PLATFORM=offscreen` 并执行 `python -m tinyforge.gui --smoke-test`，离屏验证应用、
主窗口和核心控件的构造与销毁。该 smoke test 不发起模型请求，真实窗口仍需在目标 DPI
和系统主题下做一次布局检查。

停止按钮使用 `threading.Event` 进行协作式取消。Agent 在模型调用和每项工具执行前后检查，
并把同一信号传给命令执行器；后者会终止 Windows Job Object 或 POSIX 进程组。未执行的
tool call 会补齐取消结果以保持消息协议完整。标准库 HTTP 请求仍需先返回。

## 10. 常见面试问题

**为什么不用 Agent 框架？** 题目禁止，而且本项目的目标正是展示框架通常隐藏的消息、
工具和循环逻辑。厂商客户端本可使用，但标准库 HTTP 让运行时依赖更少、协议更透明。

**模型说完成了，为什么不能直接相信？** 模型必须用明确的终止标记报告完成或阻塞；
SOP 还必须引用结构化的成功测试证据，并在提交前重新校验顺序。命令成功仍不等于语义
一定正确，所以最终用户也能从 CLI 事件和 Git diff 独立核验。

**为什么不展示模型的隐藏思维链？** tool call、参数、执行结果和简短说明已经足以审计
行为；系统不依赖也不要求私有推理文本。

**Skill 和 Memory 有什么区别？** Skill 是用户或仓库预先编写、显式授权后按需加载的非可信
操作指南；Memory 是 Agent 根据本次真实执行证据暂存并在成功收尾后提交的经验。加载 Skill
本身不构成 Memory 证据，失败归因报告也只供审计，不会自动修改 Skill；两者都不能改变工具
权限。

**下一步会改进什么？** 优先增加流式响应、用户逐条审批策略、基于厂商 tokenizer 的预算、
可恢复会话日志，以及 Docker 执行后端，而不是继续增加相似的文件工具。
