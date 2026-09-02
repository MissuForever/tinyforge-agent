# TinyForge

TinyForge 是一个不依赖 Agent 框架的本地编程智能体。它直接调用 OpenAI 兼容的
Chat Completions 或 Responses API，解析模型原生 tool calling，在本机完成文件检索、
精确编辑和命令执行，再把结果送回模型，循环直到任务完成。

项目的重点不是封装现成产品，而是用少量、可审计的代码完整展示 Agent Runtime：
消息管理、工具协议、执行循环、上下文控制、错误恢复和终止策略均由项目自行实现。

## 功能

- `list_files`：分层浏览项目，自动忽略常见大目录
- `read_file`：按行读取 UTF-8 文件，返回稳定行号
- `search_files`：纯 Python 文本或正则搜索，无外部命令依赖
- `write_file`：创建文件，自动建立父目录
- `edit_file`：精确局部替换，避免意外修改多处内容
- `run_command`：在工作区执行 PowerShell 或 POSIX shell 命令
- 单任务与连续对话两种 CLI 使用方式
- PySide6 桌面操作台：执行时间线、代码 Diff、记忆、命令输出和工作区文件总览
- 工具异常结构化回传，模型可读取错误并自行修正
- 命令超时、工作区路径隔离、危险命令拦截
- 工具输出截断、对话上下文裁剪、最大轮数和重复调用保护
- 有界 Working Memory，持续保留目标、约束、进度、证据和下一步
- L1 索引、L2 事实、L3 SOP、L4 脱敏会话档案组成的分层持久记忆
- `No Execution, No Memory` 证据门禁和失败任务不晋升策略
- Chat Completions 与 Responses API 的 Token、请求数、工具数和耗时统计
- 核心 Runtime 和 CLI 零第三方依赖；GUI 仅额外依赖 PySide6

## 快速开始

1. 准备配置：

```powershell
Copy-Item .env.example .env
```

编辑 `.env`：

```dotenv
TINYFORGE_API_KEY=your-api-key
TINYFORGE_BASE_URL=https://api.openai.com/v1
TINYFORGE_MODEL=gpt-4o-mini
TINYFORGE_WIRE_API=chat_completions
```

Responses API 配置示例：

```dotenv
TINYFORGE_API_KEY=your-api-key
TINYFORGE_BASE_URL=https://your-compatible-provider.example/v1
TINYFORGE_MODEL=your-model
TINYFORGE_WIRE_API=responses
TINYFORGE_REASONING_EFFORT=xhigh
TINYFORGE_STORE_RESPONSES=false
TINYFORGE_MEMORY_ENABLED=true
TINYFORGE_ARCHIVE_SESSIONS=true
```

也可以使用任何支持上述协议和原生 tool calling 的兼容网关。`.env` 已经被 Git 忽略；
环境变量优先于文件配置。配置文件会解析到独立映射，不写入子进程继承的全局环境。
命令行可通过 `--wire-api` 和 `--reasoning-effort` 临时覆盖。

2. 直接运行单个任务：

```powershell
py -3 -m tinyforge "阅读当前项目，找到一个值得修复的问题，修改后运行测试"
```

操作其他工作区：

```powershell
py -3 -m tinyforge -w D:\path\to\project "修复失败的测试，不要修改测试文件"
```

不提供任务文本时进入连续对话模式：

```powershell
py -3 -m tinyforge
```

启动图形界面：

```powershell
py -3 -m pip install -e ".[gui]"
py -3 -m tinyforge.gui
```

可通过 `-w D:\path\to\project` 指定初始工作区。GUI 使用 PySide6 和 Qt Widgets，Qt 6
默认跟随操作系统的高 DPI 与逐显示器缩放；Agent 在单独的后台线程运行，工具事件通过
队列送回 GUI 线程，因此模型请求和命令执行不会冻结窗口。界面不会显示或保存 API Key，
配置仍来自环境变量或未入库的 `.env`。`Terminal` 标签会按行显示 Agent 执行的命令、
标准输出、错误输出和退出码；它是只读审计视图，不提供任意命令输入入口。`Files` 标签
在后台索引 Git 可见文件，支持文件名过滤、Git 状态和带行号的只读预览；非 Git 工作区
会使用有界扫描，敏感路径、缓存目录、目录链接、二进制文件和过大文件不会自动读取。

需要保留主屏工作时，可让窗口直接在副屏居中打开，无需手动拖动：

```powershell
py -3 -m tinyforge.gui --secondary-screen
```

该模式首次显示时不抢键盘焦点、不会置顶；点击副屏窗口后仍可正常交互。没有副屏时会回退
到系统默认显示器。

交互模式支持 `/new` 清空上下文、`/memory` 查看工作记忆与 L1 索引、`/help` 查看命令、
`/exit` 退出。也可以安装命令行入口：

```powershell
py -3 -m pip install -e .
tinyforge --help
py -3 -m pip install -e ".[gui]"
tinyforge-gui --help
```

## 运行流程

```text
用户任务
   -> Working Memory 锚点 + 小型 L1 记忆索引
   -> 模型请求（压缩历史 + JSON Schema 工具定义）
   -> assistant 文本或 tool_calls
   -> 校验工具名与 JSON 参数
   -> 本地执行工具；命令输出以有界事件实时送往 GUI
   -> 生成有界的结构化工具结果
   -> 结果作为 tool 消息加入历史
   -> 继续调用模型，直到明确的 TASK_COMPLETE/TASK_BLOCKED 或触发终止条件
```

主要模块：

- `tinyforge/agent.py`：Agent 循环、事件、循环终止
- `tinyforge/model.py`：两种兼容协议的请求转换、HTTP 调用与响应解析
- `tinyforge/tools.py`：工具定义、参数处理和本地执行
- `tinyforge/context.py`：按完整工具轮次裁剪历史
- `tinyforge/memory.py`：工作检查点、分层存储、按需检索和证据门禁
- `tinyforge/config.py`：环境变量、`.env` 和运行限制
- `tinyforge/cli.py`：终端展示和交互会话
- `tinyforge/runtime.py`：CLI 与 GUI 共用的运行时装配
- `tinyforge/gui_support.py`：后台任务、事件隔离、脱敏和文件 Diff
- `tinyforge/workspace_view.py`：GUI 文件索引、Git 状态和安全预览
- `tinyforge/gui.py`：基于 PySide6 / Qt Widgets 的桌面界面

更完整的设计说明和面试问答见 [`docs/design.md`](docs/design.md)，录制流程见
[`docs/demo-script.md`](docs/demo-script.md)。论文机制、实现映射和证据边界见
[`docs/paper-improvements.md`](docs/paper-improvements.md)。

## 分层记忆

持久记忆默认保存在 `~/.tinyforge/workspaces/<workspace-hash>/`，不写入目标仓库：

```text
index.json       L1：默认注入的有界标题/关键词索引
facts/*.json     L2：经工具证据验证的稳定事实
sops/*.json      L3：经验证的可复用工作流程
sessions/*.json  L4：脱敏并限长的任务档案，不默认注入上下文
```

Agent 通过 `recall_memory` 按需读取 L2/L3，并返回条目自带的证据摘要。`stage_memory` 只暂存
候选；模型以 `TASK_COMPLETE:` 明确结束且收尾证据复检通过后才提交，失败任务则丢弃。
SOP 必须引用最后一次文件修改或未验证命令之后、可识别的测试/检查命令证据。可使用
`--no-memory` 做无记忆对照，或用 `--no-session-archive` 保留 L1-L3 但关闭 L4。

## 安全边界

文件工具会解析真实路径并拒绝工作区外的路径，包括通过 `..` 和已存在符号链接逃逸。
命令始终以工作区或其子目录作为当前目录，默认超时 60 秒。TinyForge 会拦截强制重写
Git 历史、递归强制删除、磁盘格式化和关机等明显高风险命令。只有用户明确检查后传入
`--allow-dangerous` 才会放行。

该拦截是降低误操作风险的防线，不是操作系统级沙箱。不要在包含重要未备份文件或高权限
凭据的目录中运行不可信模型；更强隔离应使用容器或受限系统账户。L4 会话档案可能包含
源码和命令输出，虽然会限制长度并过滤常见凭据，敏感项目仍应关闭档案或使用隔离状态目录。

## 测试

项目使用标准库 `unittest`，不需要安装测试框架：

```powershell
py -3 -m unittest discover -s tests -v
```

测试覆盖配置加载、模型响应解析、路径越界、文件读写与精确编辑、文本搜索、命令执行、
危险命令拦截、超长结果、上下文裁剪以及完整的模型-工具两轮闭环。CLI 集成测试还会启动
本地 OpenAI 兼容 HTTP 端点和真实子进程，验证请求、工具执行、结果回传及最终退出状态。
记忆测试覆盖跨实例复用、工作区隔离、L1 上限、证据门禁、后置验证及档案脱敏。
GUI 测试还覆盖文件树懒展开、过滤、Git 状态、敏感路径隐藏、二进制拒绝、后台结果隔离和
Agent 修改后的自动刷新。

安装 GUI 可选依赖后，可在没有显示器的 CI 或远程环境中执行离屏窗口 smoke test：

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
py -3 -m tinyforge.gui --smoke-test
Remove-Item Env:QT_QPA_PLATFORM
```

该检查只验证 Qt 应用、主窗口和核心控件能够完成构造与销毁，不会发起模型请求。实际界面
仍应在系统缩放开启的显示器上检查一次，确认字体、分栏和按钮在目标 DPI 下完整显示。

## 两分钟演示

完整 GUI 录制与神经语音渲染依赖可单独安装，不影响核心 Runtime：

```powershell
py -3 -m pip install -e ".[demo]"
```

默认渲染优先使用联网的 `zh-CN-XiaoxiaoNeural`，失败时整批回退到 Windows 已安装的本地
语音；最终 MP4 的音轨可完全离线播放。可复现录制、严格两分钟渲染和代理参数见
[`docs/demo-script.md`](docs/demo-script.md)。

生成一个带真实缺陷和测试的独立工作区：

```powershell
py -3 scripts/prepare_demo.py
py -3 -m unittest discover -s .demo/order_total/tests -t .demo/order_total -v
```

第一次测试应失败。随后运行：

```powershell
py -3 -m tinyforge -w .demo/order_total "阅读 README 和测试，修复订单总价计算；不要修改测试，运行全部测试验证结果。"
```

演示结束后再次运行测试，展示全部通过。重复录制前执行
`py -3 scripts/prepare_demo.py --force` 即可恢复初始缺陷。

## 已知边界

- 当前实现非流式 Chat Completions 与 Responses API，不包含流式事件解析。
- GUI 停止操作采用协作式取消：会终止正在执行的命令进程树并阻止后续步骤；正在执行的
  HTTP 请求仍需先返回。
- 上下文使用中英文加权估算而非厂商 tokenizer，仍可能与实际计费 Token 存在偏差。
- 长期记忆依赖模型主动形成高质量候选，尚未实现冲突过期和自动代码化技能。
- 危险命令检测基于规则，不能代替容器级隔离。
- 工具面向文本项目，不读取或编辑二进制文件。

## License

MIT
