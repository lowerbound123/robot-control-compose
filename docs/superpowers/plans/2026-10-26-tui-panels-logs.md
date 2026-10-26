# TUI panel and log refinements

**Goal:** 实现用户明确指定的相机上边框拖拽、monitor 紧凑列与自动滚动条、UTC+8 日志时间及来源/级别颜色。
**Architecture:** 保留 Textual 8.2.8 和现有后台日志排版。CameraPanel 捕获顶边鼠标拖拽，用屏幕绝对坐标调整高度；App 提供剩余高度预算。MonitorPanel 使用8字符列与自动横向溢出。新增无 UI 依赖的日志时间/来源格式助手；TUI 用 Rich Text span 保留复制和选择语义。
**Tech Stack:** Python3.10+、Textual/Rich、pytest/Pilot。
**Spec:** 本轮用户五项要求；直接沿用现有布局。两版同步；按用户后续要求，修复日志选区并完成验证后提交、推送。

## Global constraints
- 既有 Compose 权限修复和两份 Word 文档保持。两版独立安装，不互相 import。
- 相机最小4行；手动高度随终端/monitor/编辑器变化限制，保留日志和命令区。隐藏/卸载释放鼠标。
- monitor列10->8，总内容宽7+14*8=119；过宽数值使用能容纳的科学计数法，不截断相邻单元格。
- 日志time字段改为ISO8601毫秒精度且显式+08:00，TUI/headless兼容旧numeric输入。显示层省略时区后缀，级别用四字符缩写；图像和控制计时保持原格式。
- 颜色仅为展示；JSON折叠/原文复制/跨行选择、特殊字符清理不变。来源明确标记RC、MCP或policy ID。
- 遵守AGENTS：原工作区修改，.codex/task-state.md为唯一账本；无控制/推理工作进入UI线程。

## Tasks
1. 先添加真实鼠标拖拽/越界/窗口缩小/隐藏取消测试，及monitor全列可见、窄屏滚动、大数值宽度测试；观察RED。实现tui_panels.py和tui.py，GREEN。
2. 先添加固定epoch/UTC边界、日志队列/历史/JSONL同一ISO时间、TUI级别/来源配色及复制测试；观察RED。实现log_format.py、cli/controller以及structured_log/log_tui，GREEN。
3. 同步两版独立文件、更新说明。跑两版完整套件，构建Compose主/测试镜像，实际mock和跨容器/ROS回归。生成终端界面预览检查布局。完成一次独立只读审查，修复发现并验证。

## Review focus
- 顶边命中/按键/鼠标捕获、拖拽跨面板、隐藏后继续移动、缩小终端与展开编辑器。
- 宽度边界、负值/大数值/科学计数法不挤入相邻列，滚动条扩大后消失。
- UTC+8跨日、epoch0、旧字符串；日志写失败fallback也用同一格式。
- JSON原文、伪markup/控制字符、多行和长记录，Rich span不改变逻辑偏移。
- 两版差异与既有Compose未提交修改不被覆盖；旧记录JSON time消费者需适应字符串。
