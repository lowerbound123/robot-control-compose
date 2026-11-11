# 统一命令树

本地 TUI、headless 和 MCP `command(line=...)` 使用同一命令树。旧的 set/show/switch/save/load、save-config、retry-reload、quit、裸策略命令及实例前缀别名不再兼容。Tab 补全跟随命令层级。

```text
help
help config rc set
help connector switch
status
pause
resume
reset
exit

config rc show
config rc set frequency_hz 50
config rc save

connector list
connector status
connector switch mock
connector retry

global show
global show task
global set task {"phase":"grasp"}
global remove task

pose save home
pose restore home
snapshot

record
record start
record stop
record status

camera
camera show
camera hide
camera toggle
monitor
monitor example-1

policy help
policy help example set-message
policy list
policy switch example
policy switch example --reset
policy manage
policy manage save
policy manage apply
policy manage close
policy example config show
policy example config set params.message 新的消息
policy example config save
policy example retry
policy example set-message "新的消息"

mcp help
mcp help send_command
mcp take
mcp return
mcp state {"include_images":false}
```

`config rc set` 只修改内存中的冷配置草稿，不重建或修改当前 RC。`config rc show` 返回 active、draft 和 changes；changes 按字段路径给出 active/draft 值。`config rc save` 将草稿写入 RC 配置文件，重启后生效。未保存草稿在退出后丢弃。

`policy <实例> config set <key> <raw_value>` 保留 key 之后完整原文，由策略 `configure(context, candidate, key, value, callback) -> None` 解析。策略可以普通提交、请求 reset，或请求原进程 reinitialize。reset/reinitialize 完成后保持暂停，需显式恢复。显示和保存使用已提交配置，保存目标必须是明确实例。重复来源按出现顺序使用 -0/-1/...，不会把来源名隐式指向某个实例。

`connector switch <名称>` 使用命名 connector profile 热切换连接，正常 policy 容器保留；成功后暂停，失败后仅显式 `connector retry` 或 `policy <实例> retry` 恢复。profile 结构及退出证明要求见 [connector 热切换](connector-switching.md)。

`camera` 等同于 `camera toggle`，连续执行两次分别显示和隐藏相机面板。`camera show` 与 `camera hide` 显式设置显示状态。

`resume` 只恢复 policy 拥有控制权时的运行，不隐式归还 MCP 控制权。本地接管/归还使用 `mcp take` / `mcp return`；HTTP 的结构化工具名仍为 take/return。MCP 工具参数只能是 JSON 对象，无参数工具可省略对象。`mcp help` 显示工具目录。

裸 `record` 切换录制，带参数只允许 start/stop/status；on/off/toggle 仅属于结构化 MCP record 工具的 action 值。录制不改变控制归属或运动。`monitor` 切换机器人面板，`monitor <实例>` 切换该实例面板；不接受 on/off 参数。

camera、monitor、snapshot 和 policy manage 是本地界面操作，人工文本批次不接受这些路径，也不允许递归 mcp。文本批次仍逐行 FIFO，exit 只能在末行。所有固定命令树关键字均为自定义策略命令保留字，policy 根下 help/list/switch/manage 也不能用作来源 ID。

## 输入辅助（RC/SDK 0.15）

本地 TUI 输入非空前缀后，会以淡色显示最近匹配的历史命令。只有光标位于末尾、没有选区且候选菜单关闭时显示；按右方向键接受整条虚影，Enter 只提交已输入的文字。Esc 隐藏当前虚影，继续编辑后重新匹配。菜单关闭时，上下方向键仍浏览历史，返回最新位置恢复之前的输入草稿。

Tab 在输入框上方打开当前位置的候选菜单，只有一个候选也需要确认。菜单最多显示六行，随终端宽度分列，详情显示选中项的完整名称、说明及参数类型。

| 菜单打开时 | 行为 |
| --- | --- |
| Tab / Shift+Tab | 下一项 / 上一项，循环选择 |
| 上下左右 | 在候选行列间移动；单列时上一项 / 下一项 |
| 字符、退格、粘贴 | 编辑原文本并更新候选 |
| 空格 / Enter | 确认选中项并关闭菜单，不执行命令 |
| 再次 Enter | 通过原提交入口执行实际文本 |
| Esc | 关闭菜单并保留文本 |
| Home / End、鼠标定位 | 关闭菜单，再移动光标 |

确认只替换光标所在参数，其他参数保留。普通命令参数自动补足分隔空格；配置的最终原始值不追加空格；目录补 `/`，再次 Tab 列出其下一级。没有匹配项时，Enter 只关闭菜单，空格正常输入。候选确认和接受虚影不会打断人工文本批次，真正的非空本地提交沿用批次中断规则。

参数格式、枚举和配置项提示来自静态声明。提示不阻止提交；未声明的 policy 配置项标为“未在提示声明中”，仍由 `configure` 解析完整原始值。注册命令原有参数数量、类型转换及 MCP 类型检查仍生效。

路径候选在路径所属环境查询：`pose restore` 从 RC 当前 outputs 的 `state_saves` 目录列举；policy 路径在目标实例容器内列举，不自动启动实例。目录优先，支持文件后缀过滤；默认隐藏点文件，输入点前缀时显示。补全限制在声明根目录内，手动输入的其他路径继续交给原执行逻辑。不可用、无权限或超时只影响提示。

历史保存在对应 outputs 的 `command-history/<配置路径摘要>.json`，每个 RC 配置文件独立保留最多 500 条，完全重复项移到最近位置。只记录通过语法和敏感字段检查的本地提交，保留原始空格和引号；之后业务执行失败仍保留。读写在后台进行，原子替换，文件权限为当前用户读写。损坏或不可写会提示但不影响运行；退出只在清理期限内短暂等待最后一次保存。
