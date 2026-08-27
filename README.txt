TinyForge 编程智能体

Git 仓库：[提交前填写公开 GitHub 或 Gitee 仓库地址]

运行环境：Python 3.10 及以上。复制 .env.example 为 .env，填写 TINYFORGE_API_KEY、TINYFORGE_BASE_URL 和 TINYFORGE_MODEL，然后执行：
py -3 -m tinyforge "你的编程任务"
操作其他项目可增加 -w 项目路径；不提供任务则进入连续对话。运行测试：
py -3 -m unittest discover -s tests -v

特色：项目不依赖任何 Agent 框架或服务端文件工具，自行实现兼容 Chat Completions 和 Responses API 的原生 tool calling 循环。Agent 可浏览、读取、搜索、创建和精确编辑本地文件，并执行测试或构建命令。工具错误会结构化回传给模型，使其能够自行修正。文件路径被限制在工作区内，命令具有超时和危险操作拦截；同时具备工具输出截断、上下文裁剪、最大轮数和重复调用保护。

演示：执行 py -3 scripts/prepare_demo.py 生成带缺陷的订单计价项目，再让 TinyForge 阅读需求和测试、修复实现并运行测试验证。详细设计、限制和安全说明见仓库 README.md 与 docs/design.md。
