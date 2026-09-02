TinyForge 编程智能体

Git 仓库：https://github.com/MissuForever/tinyforge-agent

环境：Python 3.10 及以上。复制 .env.example 为 .env，填写 TINYFORGE_API_KEY、TINYFORGE_BASE_URL、TINYFORGE_MODEL。运行：
py -3 -m tinyforge "你的编程任务"

GUI：先执行 py -3 -m pip install -e ".[gui]"，再执行 py -3 -m tinyforge.gui；副屏启动加 --secondary-screen。左侧显示工作区，命令输出位于中间下方，右侧显示详情、Diff、记忆和 Skills。

Skills 默认关闭。用户级目录为 ~/.tinyforge/skills，工作区目录为 <workspace>/.tinyforge/skills。CLI 加 --skills 才授权当前会话，--skills-dir 只替换用户级目录；GUI 的 Skills 复选框执行同一授权，只读面板显示任务 Top-K、加载 receipt、资源读取和失败归因。加载顺序是 list_skills 元数据、load_skill 说明、read_skill_resource 快照文本。内容标记为不可信，不增加 Agent 权限，scripts 不会被 Skill 工具自动执行；失败报告不自动判断责任或修改 Skill。启用前应审查来源，不要保存密钥。

示例：py -3 -m tinyforge --skills -w . "使用 workspace:verified-bugfix 修复可复现缺陷，并给出修改前后的测试证据"

测试：py -3 -m unittest discover -s tests -v
离屏 GUI：$env:QT_QPA_PLATFORM="offscreen"; py -3 -m tinyforge.gui --smoke-test

项目自行实现 tool calling、文件命令工具和分层记忆。演示用 py -3 scripts/prepare_demo.py；设计与安全边界见 README.md。
