TinyForge 编程智能体

Git 仓库：https://github.com/MissuForever/tinyforge-agent

环境：Python 3.10 及以上。复制 .env.example 为 .env，填写 TINYFORGE_API_KEY、TINYFORGE_BASE_URL、TINYFORGE_MODEL。运行：
py -3 -m tinyforge "你的编程任务"

GUI：先执行 py -3 -m pip install -e ".[gui]"，再执行 py -3 -m tinyforge.gui；副屏启动加 --secondary-screen。左侧的 Files / History 可浏览工作区并恢复、重命名或删除历史会话，命令输出位于中间下方，右侧显示详情、Diff、记忆和 Skills。

CLI 历史命令：/history 列表，/open ID 恢复，/rename ID TITLE 重命名，/delete ID 删除。会话默认保存在 ~/.tinyforge/workspaces/<workspace-hash>/sessions/，不会写入项目仓库。

记忆默认开启，并按工作区隔离保存在 ~/.tinyforge/workspaces/<workspace-hash>/。Working Memory 持续记录当前目标、约束、进度、证据和下一步；持久记忆分为 L1 有界索引、L2 已验证事实、L3 可复用 SOP 和 L4 脱敏会话档案。Agent 使用 recall_memory 按需检索，使用 stage_memory 暂存候选；只有任务明确完成且通过最终证据复检才会提交，失败任务不会晋升长期记忆，遵循 No Execution, No Memory。会话归档与长期记忆开关相互独立。

Skills 默认关闭，使用 --skills 或 GUI 的 Skills 复选框按会话授权。Agent 先通过 list_skills 检索任务相关的用户级或工作区 Skill，再按需调用 load_skill 和 read_skill_resource；GUI 会显示 Top-K 结果、加载顺序和内容哈希。Skill 内容只读且视为不可信，不会增加 Agent 的工具权限，也不会自动执行其中的脚本。

记忆演示：py -3 -m tinyforge -w .；完成一次带测试证据的修复后输入 /memory 查看工作记忆与 L1 索引，再开始新任务验证可复用记忆。

Skill 演示：py -3 -m tinyforge --skills -w . "使用 workspace:verified-bugfix 修复可复现缺陷，并给出修改前后的测试证据"

测试：py -3 -m unittest discover -s tests -v
离屏 GUI：$env:QT_QPA_PLATFORM="offscreen"; py -3 -m tinyforge.gui --smoke-test

项目自行实现 tool calling、文件命令工具和分层记忆。演示用 py -3 scripts/prepare_demo.py；设计与安全边界见 README.md。
