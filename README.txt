TinyForge 编程智能体

Git 仓库：https://github.com/MissuForever/tinyforge-agent

运行环境：Python 3.10 及以上。复制 .env.example 为 .env，填写 TINYFORGE_API_KEY、TINYFORGE_BASE_URL 和 TINYFORGE_MODEL，然后执行：
py -3 -m tinyforge "你的编程任务"
核心 Runtime 和 CLI 无第三方运行时依赖。图形界面需先执行 py -3 -m pip install -e ".[gui]"，再执行 py -3 -m tinyforge.gui；需要直接在副屏打开且不抢焦点时执行 py -3 -m tinyforge.gui --secondary-screen。PySide6 界面可实时查看执行时间线、工具详情、代码 Diff 和记忆概览。操作其他项目可增加 -w 项目路径；不提供任务则进入连续对话。运行测试：
py -3 -m unittest discover -s tests -v
离屏 GUI 检查（PowerShell）：$env:QT_QPA_PLATFORM="offscreen"; py -3 -m tinyforge.gui --smoke-test; Remove-Item Env:QT_QPA_PLATFORM

特色：项目不依赖任何 Agent 框架或服务端文件工具，自行实现兼容 Chat Completions 和 Responses API 的原生 tool calling 循环。Agent 可读写代码、运行测试并根据反馈继续修复。参考 GenericAgent 论文实现有界工作检查点和 L1 索引、L2 事实、L3 SOP、L4 会话档案；深层记忆按需读取，只有带成功工具证据且任务最终成功的知识才会提交。系统还具备工作区隔离、命令超时、危险操作拦截、分层上下文压缩、循环保护和 Token/耗时统计。

演示：执行 py -3 scripts/prepare_demo.py 生成带缺陷的订单计价项目，再让 TinyForge 阅读需求和测试、修复实现并运行测试验证。详细设计、限制和安全说明见仓库 README.md 与 docs/design.md。
