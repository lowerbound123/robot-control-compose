# MCP 管理与控制

WebUI 使用的 `status` 额外返回本次 RC 的 `run_id`。`take`、`return`、`command`、`record`、`policy_command`、`policy_config`、`policy_chain`、`retry_reload`、`return_and_wait`、`send_command`、`move_eef`、`move_joints` 支持可选 `expected_control={run_id, control_epoch, generation}`：RC 对提交时状态进行原子复核，普通排队控制命令在执行前再次复核。浏览器可据此拒绝翻译期间人工干预或重启前生成的旧操作。不传时沿用原有行为；录制仍不要求接管。终态回执新增不可变的 `completion_control`（相同三个字段），记录完成时的控制状态；`controller` 仍是查询时的当前快照，不能用它替代完成证明。见 [WebUI](webui.md)。

服务默认提供 Streamable HTTP `/mcp`。本地 TUI/headless 的 `mcp <工具> <JSON>` 使用同一组注册工具。`mcp help` 查看完整参数；MCP `help()` 查看 RC 帮助。工具参数只能是 JSON 对象，无参数时可省略对象；不支持位置参数简写。TUI 接管和归还分别使用 `mcp take` / `mcp return`，裸 take/return 拒绝。`resume` 不会归还 MCP 控制权。

## 人工文本批次

`tui_command(text, wait=false, timeout_s=30, expected_control=None)` 是供人工 WebUI 使用的入口，按本地 TUI 的 RC/policy 命令语义执行，默认无需 `take`。**建议 Agent 不要使用此入口，应使用原有结构化工具及 take 流程。** 工具不会自动 take/return，也不会改变控制归属；TUI 自身的控制权冲突与生命周期检查仍适用。其他 MCP 工具的权限和调度不变。

`text` 每个非空行是一条命令，LF/CRLF 均可，保留引号和 JSON 参数；不执行 shell，不按分号拆分，不支持跨行 JSON。单批最多 4000 字符、64 条命令，最多 32 个未结束批次。整批预检拒绝递归 `mcp ...`、本地面板及 `snapshot`、非法引号和敏感配置写入。`exit` 只允许末行；`quit` 已移除。策略与实例在每行执行时解析，所以可以先 policy switch 再调用新实例的命令。

```json
{"text":"policy example config set params.message \"人工输入\"\npolicy example example","wait":false}
```

文本批次按 FIFO 执行，逐行等待成功回执；失败或取消停止本批剩余行，不回滚完成的操作，之后继续下一批。真实本地 TUI/headless 的每次非空提交（含查询及本地面板输入）清除当前批次未开始的行和所有等待批次；已开始的行按原生命周期收尾。批次内部使用 TUI 语义不会触发此打断规则。

父 `operation_id` 可通过 `get_operation` 查询；`result.kind="tui_batch"`，`steps` 包含原行号、命令、子操作 ID、状态及结果，`completed/total` 表示进度。`cancel_tui_command(operation_id)` 幂等停止指定批次后续行，不影响其他批次。活动批次等待当前已执行行收尾后才进入终态。超时或断连只停止等待，不取消、不重发；`expected_control` 只在整批受理时核对，后续行遵守执行时的 RC 状态。末行退出的结果以实际统一清理结果为准。

## 策略发现与命令

`policy_commands(policy_id=None)` 返回 `policies` 列表及无效目录的 `errors`；每项包含 `source_id`、`instance_id`、`loaded`、`runnable` 和 `commands`。命令列出名称、参数类型、说明、执行时限、effect 及 `requires_take`。已加载实例使用当前配置快照；未加载来源从 manifest 查询，不导入模型。查询重复来源返回各实例，执行时必须明确一个实例。`help(policy_id="example", command="reinitialize")` 可进一步筛选命令。

```text
mcp policy_commands {"policy_id":"example"}
policy help example set-chunk-size
mcp take
mcp policy_command {"instance_id":"example","command":"set-chunk-size","args":[6],"wait":true}
mcp return
```

结构化参数使用 JSON 原生类型，不把字符串转成数字、不把布尔值当整数。`command("policy example set-chunk-size 6")` 沿用 TUI 的字符串解析；裸命令及实例前缀别名拒绝。`command("policy help example")` 可定向查询。Agent 优先使用结构化工具。

## 策略配置

`policy_config(instance_id, action="show"|"set"|"save", key=None, value_text=None, wait=false, timeout_s=30, expected_control=None)` 使用明确实例。show 只读；set/save 要先 take。set 的 value_text 是原始文本，例如 `{"instance_id":"example-1","action":"set","key":"params.message","value_text":"保留空格的消息"}`，由策略 configure 解析，不在 MCP 层先转为 JSON 值。成功后保留 MCP 控制权，policy 暂停。show 返回已提交配置，save 写回来源目录。

## 直接提交链

```text
mcp take
mcp policy_chain {"policies":["example","example"],"wait":true}
mcp policy_commands {"policy_id":"example"}
mcp policy_command {"instance_id":"example-1","command":"reinitialize","wait":true}
mcp policy_chain {"policies":["example","example"],"action":"save","wait":true}
mcp return {"wait":false}
```

`policy_chain` 的 `action` 默认 `apply`：列表不能为空，成员是来源名，可重复，依次生成 `example-0`、`example-1` 等独立实例。应用为 `default_chain`，从各来源配置重建全组，保持暂停与 MCP 控制权，不复位、不落盘；相同列表再次应用也重建全组。无效来源、嵌套链、控制模式不匹配和名称冲突在旧组退出前拒绝。进入生命周期变更时取消手动动作并保持当前目标；旧组退出和 reader 释放确认齐备后才创建新组。

`action="save"` 只把给定列表写入 `policy/default_chain/policy.yaml`，不改变当前运行组，不写成员运行参数。链列表使用该结构化工具保存；保存成员参数使用 `policy <精确实例> config save`。带 UI 草稿的 `policy manage` 保持本地使用。

恢复运行通常使用 `return(wait=false)`，通过 `get_operation` 确认归还完成后让 policy 持续运行。只有当前 policy 或链内成员明确会为本次任务调用 `runtime.finish`，且需要等待该业务结果时，才选择 `return_and_wait` 并通过 `get_policy_result` 查询。连续输出动作的 policy 没有必然的业务终态；`return_and_wait(timeout_s=0)` 也会创建等待任务，不能代替普通 no-wait return。

## 失败与重试

`reload_policy` 只重建目标实例，`reload_all_policy` 重建全组，`reinitialize_policy` 复用目标 PID 和已加载模块。失败后查看 `status.retry_reload_instance`；有候选时，MCP 可在接管状态调用 `retry_reload(instance_id="example-1")`，或 `command("policy example-1 retry")`。仅重建该失败目标，健康成员继续驻留。成功后仍暂停并由 MCP 持有控制权；不会自动重试或恢复运动。

## 录制与回执

`record(action="status")` 默认只读查询。`on/off/toggle` 是无需 `take` 的明确例外，可在 policy 运行和生命周期忙碌时使用，不改变控制归属、动作代次或推理，不取消 policy 等待任务。`off` 成功意味着已接收数据完成落盘，详见 [录制说明](recording.md)。

新增执行工具统一默认 `wait=false`、`timeout_s=30`，返回操作回执并可通过 `get_operation` 继续查询。等待超时或客户端断连不取消已受理操作；manifest 的 handler 时限独立生效。所有写操作有 MCP 请求和终态日志，失败含原因，输出继续脱敏；能力、状态等只读工具不新增轮询日志。所有客户端共享同一控制权，旧代次请求不能在归还、暂停或切换后提交。
