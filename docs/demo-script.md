# PySide6 GUI 演示与成片流程

这套流程录制真实的 TinyForge 桌面操作台，不使用伪造事件或预先写好的修复结果。录制脚本
会准备独立缺陷项目、启动 PySide6 GUI、调用真实模型完成修复，并自动回看测试、Diff、
文件总览、Skill receipt、记忆和最终结果；渲染脚本再加入中文旁白、标题卡和总结卡。

## 录制前

1. 在 Windows 上激活 GUI 环境并安装项目、PySide6 与渲染依赖：

```powershell
conda activate tinyforge-gui
python -m pip install -e ".[demo]"
```

2. 在 `.env` 中配置可用模型，确认 `.env` 被 Git 忽略。不要在终端、GUI、视频或截图中
   打开 `.env`，也不要把 API Key 写进命令行参数。
3. 确认录屏所需 FFmpeg 位于：

```text
.demo/video-tools/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe
```

   渲染器还使用 Windows 的 Noto Sans SC 和 Cascadia Mono 字体。旁白默认使用联网的
   `zh-CN-XiaoxiaoNeural` 神经语音；断网时 `auto` 模式会整批回退到本地已安装的
   `Microsoft Huihui Desktop`，不会在同一视频中混用音色。
4. 关闭通知和可能显示个人凭据的窗口，保持显示器布局和系统缩放稳定。默认模式会自动选择
   副屏；`--primary-fullscreen` 会覆盖主屏、置顶并主动获取焦点，两种模式都不要求手动拖动。
5. 运行主项目测试，预期全部通过：

```powershell
python -m unittest discover -s tests -v
```

6. 可先执行 PySide6 离屏 smoke。正式录制前必须删除 `QT_QPA_PLATFORM`，否则系统中不会
   出现可供 FFmpeg 捕获的窗口：

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
python -m tinyforge.gui --smoke-test
Remove-Item Env:QT_QPA_PLATFORM
```

## 录制真实 GUI

执行：

```powershell
python scripts/record_gui_demo.py --output .demo/gui-video-layout-final --force --timeout 600
```

`--output` 必须位于 `.demo` 下；`--force` 会删除并重建指定输出目录。`--timeout` 是 Agent
运行的最长秒数，超时后脚本会请求协作式停止。

需要覆盖主屏录制时，显式增加 `--primary-fullscreen`。该模式会把窗口绑定到主显示器，进入
全屏、设置置顶并主动获取焦点：

```powershell
python scripts/record_gui_demo.py --output .demo/gui-video-primary-fullscreen --primary-fullscreen --timeout 600
```

脚本会自动完成以下演示流程：

1. 调用 `scripts/prepare_demo.py` 创建全新的 `workspace`，并建立隔离的本地 Git 基线。
2. 在录制前确认演示项目共有 4 项测试，其中 3 项失败。
3. 默认优先选择第一个非主显示器并打开约 `1020x720` 的普通窗口；显式使用
   `--primary-fullscreen` 时绑定主屏、调用 `showFullScreen()`、设置 `WindowStaysOnTopHint`，
   并验证全屏、置顶和目标显示器状态。
4. 把仓库中的 `verified-bugfix` 示例复制到隔离工作区并通过进程配置启用 Skills。自动输入
   的任务要求 Agent 先检索、加载 Skill，再建立失败基线，只修改 `pricing.py`，重新执行完整
   测试，并使用 `stage_memory` 保存带验证证据的 SOP。
5. 在中间区域同步展示执行时间线，以及下方独立的命令、输出和退出状态。
6. Agent 结束后依次回看最终 Result、失败测试、统一 Diff、左侧 Workspace 中的 Git 修改
   状态和 `pricing.py` 只读预览，再切换 History 展示可恢复会话，最后查看成功测试、持久记忆
   及 Skill 检索和 receipt。
7. 在 GUI 关闭后独立复跑 4 项测试，并检查只改动了 `pricing.py`。

录制成功时，终端最后输出的 JSON 必须包含：

```json
{"accepted": true, "status": "Completed", "changed_files": ["pricing.py"], "command_showcase": {"visible": true, "count": 2}}
```

录制不是按窗口标题估算画面范围：脚本通过 Win32 DWM 读取窗口的实际可见物理边界，DWM
不可用时回退 `GetWindowRect`，再把物理桌面坐标和尺寸传给 FFmpeg `gdigrab`。因此副屏偏移、
系统缩放和窗口边框都会计入抓取区域，`result.json` 也会记录 `capture_screen` 和
`capture_region` 便于复核。

录制结束时脚本会保持 TinyForge 窗口可见，先向 FFmpeg 发送停止信号并等待 MP4 封口，再
退出 GUI。不要把这两个步骤调换，否则桌面矩形捕获会在尾部录入短暂出现的后台窗口。

主要录制产物位于命令指定的 `.demo/<recording-name>/`：

- `gui-raw.mp4`：无旁白的真实 GUI 窗口录制；
- `capture-reference.png`：Qt 在录制开始前保存的窗口参考图；
- `result.json`：脱敏后的配置摘要、时间线、标记、Diff、记忆和验收数据；
- `baseline.txt`：录制前的 4 项测试失败基线；
- `verification.txt`：Agent 完成后的独立测试结果；
- `ffmpeg-record.log`：窗口捕获日志；
- `state/`：本次演示使用的隔离记忆状态。

如果 `accepted` 为 `false`，不要继续渲染。先检查 `result.json` 和日志；脚本会拒绝测试证据
不完整、命令面板不可见、改动文件不正确、记忆未提交或 FFmpeg 失败的录制。

## 演示内容

成片按以下顺序呈现，具体时间由录制标记自动计算：

- 8 秒标题卡：说明“真实模型调用、原生桌面 GUI、Skill、文件与工具证据”；
- GUI 主体：输入带约束的任务，观察 Agent 建立基线、读取代码、修改实现和执行验证；
- 证据回看：展示命令面板中的 3 项初始失败、仅含 `pricing.py` 的 Diff、Workspace 文件树
  与 Git 状态、带行号的代码预览、命令面板中的 4 项测试全过，以及 Skills 页中的检索记录、
  加载顺序与内容哈希；
- 9 秒记忆卡：展示 SOP 内容及其修改、测试证据；
- 11 秒总结卡：展示完成状态、工具统计和主项目完整测试通过。

现场讲解可集中说明：模型负责选择工具和生成参数，本地 Runtime 负责路径隔离、文件编辑、
命令执行和结构化回传；GUI 只是同一 Runtime 的观察与操作层。持久记忆需要成功执行证据，
失败任务不会晋升经验。

## 渲染最终视频

使用录制结果生成带中文旁白的 1080p 成片：

```powershell
python scripts/render_gui_demo_video.py `
  --input .demo/gui-video-primary-fullscreen `
  --output .demo/gui-video-primary-fullscreen/TinyForge-GUI-Primary-Fullscreen-demo-2min.mp4 `
  --max-gui-seconds 92 `
  --tts-backend edge `
  --edge-voice zh-CN-XiaoxiaoNeural `
  --edge-rate=+5%
```

`--max-gui-seconds` 是计算 GUI 主体加速比例时使用的目标上限，标题、记忆和总结卡共额外
占用 28 秒。上面的 `92` 是严格两分钟成片示例；渲染后仍需根据 `video-manifest.json`
检查最终时长和旁白完整性。

渲染脚本会先验证 `result.json` 的完成状态、测试证据、修改范围和记忆提交，再联网生成自然
中文旁白并统一转换为 `24 kHz` 单声道 PCM。`--tts-backend edge` 在网络失败时会明确报错；
使用默认的 `auto` 会改为整批本地旁白。受限网络可增加 `--tts-proxy http://host:port`；需要
强制直连时使用 `--no-tts-proxy`。代理参数不要包含会写入命令历史的明文凭据。最终 MP4
内嵌 AAC 音轨，播放时不需要联网或安装 TTS。
脚本会检查最终文件包含 `1920x1080` H.264 视频和 AAC 音频，并输出：

- `.demo/gui-video-layout-final/TinyForge-GUI-Layout-demo-2min.mp4`：最终成片；
- `.demo/gui-video-layout-final/video-manifest.json`：时长、分辨率、帧率、加速比例、旁白时间点，
  以及请求/实际 TTS 后端、音色、语速、采样率和回退状态；
- `.demo/gui-video-layout-final/render-gui/`：标题卡、记忆卡、总结卡和旁白等中间产物。

## 提交前检查

- 主项目离线测试全部通过，GUI smoke 通过；
- 录制命令退出码为 0，终端摘要中 `accepted` 为 `true`；`result.json` 中状态为
  `Completed`，4 项演示测试最终全部通过；
- 视频中能看清执行时间线、命令面板内的失败基线与成功验证、`pricing.py` Diff、Workspace
  的 `M` 状态与代码预览、Memory 和 Result；
- 画面、声音、日志和 JSON 中没有 API Key、`.env` 内容、认证头或其他凭据；
- 最终文件为 MP4，分辨率、音视频流、时长和文件大小符合提交平台要求；
- 将 `README.txt` 中的占位地址替换为真实公开仓库地址；
- 公开仓库能从全新目录克隆，并按 README 的 `.[gui]` 安装与启动命令运行；
- ZIP 只包含提交要求的文件并按要求命名，截止时间后不再修改公开仓库。
