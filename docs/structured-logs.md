# 日志展开、复制与粘贴

本功能仅改变 TUI 的展示与交互。日志文件、headless 输出和策略通信格式不变，不需要修改 policy 或 manifest。

## 阅读 JSON 日志

在 TUI 执行 `status`、`show rc` 或 MCP 查询，可以看到 JSON 日志。每条记录第一行右侧有 `[复制]`；JSON 左侧另有 `▸`。

- 默认折叠为一个物理行，超宽预览显示 `…`。点击第一行展开，再次点击第一行收起。
- 展开后按两个空格缩进，区分字段名、字符串、数字、布尔值与 null 的颜色；长行自动换行。
- 支持纯对象/数组，也支持 `MCP state: {...}`、`policy command [A:1] succeeded: {...}` 等文字前缀。MCP 图像未显示的提示会保留。
- 一次日志事件中的多行 JSON 可以展开。分别打印的多条日志不自动拼接；不完整 JSON、普通文本和 JSON 标量继续作为文本显示。
- 不执行日志里的颜色标记或终端控制序列。`[bold]` 等内容按文字显示。

日志按完整事件保留，最多 10,000 条；展开的行数不会缩短历史。解析和排版在一个有界后台线程中进行，尚未完成时先显示原文预览。界面只绘制可见行。

## 复制

| 操作 | 结果 |
| --- | --- |
| 鼠标拖动选择，再按 **Ctrl+Shift+C** | 复制所选文字，可跨行、跨日志 |
| 点击 JSON 第一行的 `[复制]` | 复制完整原始 JSON，不包含时间、级别或前缀 |
| 点击普通日志的 `[复制]` | 复制完整原始正文 |
| 没有选区时按 Ctrl+Shift+C | 提示先选择文字，不会退出 |

拖动选择不会展开或收起记录。选区不包含折叠箭头、复制按钮和省略号；自动换行不添加额外换行。完整选择展开的 JSON 正文，可以直接粘贴为有效 JSON。复制按钮始终复制完整内容，不受预览长度限制；解析尚未完成时会等待解析后复制。后一次复制操作优先于尚未完成的复制。

只有位于底部且没有活动选区时，新日志才自动滚到底部。查看历史或选择文字时，新增日志保持当前位置。展开、收起及窗口缩放保留顶部日志；选区内容被隐藏或历史被淘汰时，会清除失效选区并短暂提示。

**Ctrl+C 和 Ctrl+Q 仍然走 RC 的安全退出流程。** 它们不是日志复制快捷键。

## 粘贴到命令框

在日志区聚焦时，用终端的粘贴快捷键（Ubuntu 通常为 **Ctrl+Shift+V**）粘贴，文本会进入底部命令框并聚焦。直接在命令框粘贴也支持多行内容。

粘贴保留所有文字和引号，将 CRLF、CR、LF 换行转为空格，在原光标或选区位置插入。粘贴不会自动执行；检查内容后，由用户按 Enter 提交。策略编辑器等其他输入组件的粘贴行为不变。

## Ghostty 与 Docker attach

复制使用终端的 OSC 52 剪贴板协议，直接交给宿主终端处理，不依赖容器里的 `xclip`、桌面服务或宿主剪贴板读取权限。终端必须允许应用写入剪贴板；TUI 无法确认宿主系统是否接受了写入。

如果 Ghostty 拦截 Ctrl+Shift+C，可在其配置文件中使用：

```ini
keybind = performable:ctrl+shift+c=copy_to_clipboard
```

这个绑定在存在终端原生选区时执行终端复制；没有原生选区时，把按键传给 TUI。需要按 Ghostty 的方式重新加载配置。本项目不会自动改动宿主配置。

也可以直接点击日志 `[复制]`，避免快捷键拦截；它同样需要终端允许 OSC 52。Shift+拖动可以使用 Ghostty 原生选区，作为屏幕文字复制的备用方式，但原生选区不了解日志的自动换行和 JSON 原文。经 SSH、tmux 等额外层连接时，还需该层允许剪贴板转发。

参考：[Textual 剪贴板接口](https://textual.textualize.io/api/app/#textual.app.App.copy_to_clipboard)、[Ghostty 快捷键说明](https://ghostty.org/docs/config/keybind)、[Ghostty 配置参考](https://ghostty.org/docs/config/reference)。

## 更新 Docker 部署

在本仓库目录构建主镜像：

```bash
docker compose build robot-control
```

先在旧 TUI 中运行 `exit`，等待控制器完成清理。IPC anchor 和 policy 服务仍在运行时，只重建主服务：

```bash
docker compose up -d --no-deps --force-recreate robot-control
docker compose attach robot-control
```

如果全栈已经停止，使用 `docker compose up -d` 启动依赖。使用 Qwen、LLM、ROS 或真实机器人覆盖配置时，以上命令必须保留当前部署完整的 `-f` 参数组合。

本次没有修改 SDK 或 policy 镜像；只需重建主镜像。镜像内 Textual 锁定 8.2.8，主包最低版本要求也为 8.2.8。重启后遵循现有暂停与显式 `resume` 流程。

## 验证

在开发测试环境执行：

```bash
python -m pytest tests/test_structured_log.py tests/test_structured_log_tui.py -q
```

自动化覆盖 JSON、选区、实际 OSC 52 输出、增强键盘事件、粘贴、滚动、拥塞和退出。真实终端还需要人工验收：

1. Ghostty 中 attach 到控制器，执行 `show rc`，展开和收起 JSON。
2. 拖选中文及跨行文字，用 Ctrl+Shift+C 粘贴到宿主编辑器，检查没有多余自动换行。
3. 点击 JSON 的 `[复制]`，确认粘贴得到完整 JSON。
4. 从宿主复制多行带引号文本，在日志区粘贴，确认命令框保留全部文本且没有执行。
5. 打开 camera 和 policy monitor，缩放窗口、查看历史日志，检查各区域正常。

宿主剪贴板、真实 Ghostty/Docker attach 的视觉和鼠标效果，必须以这套人工验收为准；模拟事件和协议输出检查不能替代它。

## 本次自动验证（2026-10-24）

| 环境 | 完整套件结果 |
| --- | --- |
| robot-control 本机 Python 3.11 | 621 通过，20 跳过 |
| robot-control-compose 本机 Python 3.11 | 606 通过，65 跳过 |
| Linux ARM64 / Humble / Python 3.10 镜像 | 583 通过，58 跳过 |

其中新增日志专项测试 42 项全部通过；现有跨容器与真实 rclpy 独立模拟器回归 6 项通过。原有 Pillow 图像接口弃用告警仍有 13 条，不影响测试结果。两套本机环境及 RC/runtime-test 镜像的依赖检查通过；镜像内三个 TUI 模块的哈希与最终源码一致。测试用 Compose 服务和卷已清理。

已修复审查发现的展开/缩放跳到底部、截断外层 JSON 误认内部对象、大选区空闲反复扫描，以及空白区误点击、旧拖动恢复失效选区的问题。没有修改 SDK、策略通信和控制器；两个工作区此前的业务改动保留。

真实 Ghostty / Docker attach 的宿主剪贴板与鼠标视觉验收仍待人工执行；本次未把模拟键鼠或 OSC 52 协议检查视为宿主剪贴板验收。镜像验证运行于本机 ARM64，未代替 x86 实机验收。
