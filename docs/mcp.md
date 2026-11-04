# MCP 管理与控制

WebUI 使用的 `status` 额外返回本次 RC 的 `run_id`。`take`、`return`、`command`、`record` 支持可选 `expected_control={run_id, control_epoch, generation}`：RC 对提交时状态进行原子复核，普通排队控制命令在执行前再次复核。浏览器可据此拒绝翻译期间人工干预或重启前生成的旧操作。不传时沿用原有行为；录制仍不要求接管。见 [WebUI](webui.md)。

服务默认提供 Streamable HTTP `/mcp`。本地 TUI/headless 的 `mcp <工具> <JSON>` 使用同一组注册工具。`help mcp` 查看完整参数；MCP `help()` 查看 RC 帮助。

## 策略发现与命令

`policy_commands(policy_id=None)` 返回 `policies` 列表及无效目录的 `errors`；每项包含 `source_id`、`instance_id`、`loaded`、`runnable` 和 `commands`。命令列出名称、可用实例别名、参数类型、说明、执行时限、effect 及 `requires_take`。已加载实例使用当前配置快照；未加载来源从 manifest 查询，不导入模型。查询重复来源返回各实例，执行时必须明确一个实例。`help(policy_id="example", command="reinitialize")` 可进一步筛选命令。

```text
mcp policy_commands {"policy_id":"example"}
mcp help {"policy_id":"example","command":"set-chunk-size"}
mcp take
mcp policy_command {"instance_id":"example","command":"set-chunk-size","args":[6],"wait":true}
mcp return
```

结构化参数使用 JSON 原生类型，不把字符串转成数字、不把布尔值当整数。`command("policy example set-chunk-size 6")`、无歧义的已注册别名与裸命令仍可使用，沿用 TUI 的字符串解析。`command("help example")` 也可定向查询；同名策略与命令优先展示策略的完整帮助。

## 直接提交链

```text
mcp take
mcp policy_chain {"policies":["example","example"],"wait":true}
mcp policy_commands {"policy_id":"example"}
mcp policy_command {"instance_id":"example-1","command":"reinitialize","wait":true}
mcp policy_chain {"policies":["example","example"],"action":"save","wait":true}
mcp return_and_wait {"timeout_s":30}
```

`policy_chain` 的 `action` 默认 `apply`：列表不能为空，成员是来源名，可重复，依次生成 `example-0`、`example-1` 等独立实例。应用为 `default_chain`，从各来源配置重建全组，保持暂停与 MCP 控制权，不复位、不落盘；相同列表再次应用也重建全组。无效来源、嵌套链、控制模式不匹配和名称冲突在旧组退出前拒绝。进入生命周期变更时取消手动动作并保持当前目标；旧组退出和 reader 释放确认齐备后才创建新组。

`action="save"` 只把给定列表写入 `policy/default_chain/policy.yaml`，不改变当前运行组，不写成员运行参数。`save-config policy` 可保存当前活动链；保存成员参数仍须 `save-config <精确实例>`。带 UI 草稿的 `manage-policy` 保持本地使用。

## 失败与重试

`reload_policy` 只重建目标实例，`reload_all_policy` 重建全组，`reinitialize_policy` 复用目标 PID 和已加载模块。失败后查看 `status.retry_reload_instance`；有候选时，MCP 可在接管状态调用 `retry_reload(instance_id="example-1")`，或 `command("retry-reload example-1")`。仅重建该失败目标，健康成员继续驻留。成功后仍暂停并由 MCP 持有控制权；不会自动重试或恢复运动。

## 录制与回执

`record(action="status")` 默认只读查询。`on/off/toggle` 是无需 `take` 的明确例外，可在 policy 运行和生命周期忙碌时使用，不改变控制归属、动作代次或推理，不取消 policy 等待任务。`off` 成功意味着已接收数据完成落盘，详见 [录制说明](recording.md)。

新增执行工具统一默认 `wait=false`、`timeout_s=30`，返回操作回执并可通过 `get_operation` 继续查询。等待超时或客户端断连不取消已受理操作；manifest 的 handler 时限独立生效。所有写操作有 MCP 请求和终态日志，失败含原因，输出继续脱敏；能力、状态等只读工具不新增轮询日志。所有客户端共享同一控制权，旧代次请求不能在归还、暂停或切换后提交。
