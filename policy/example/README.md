# 用 example 验证 RC

在仓库根目录用测试环境的 Python 运行：

```bash
python3 scripts/test_scope.py
```

默认只加载 [example](main.py)。测试使用临时配置、mock 双臂和真正的 SDK host / worker；原生链也只使用 example，不需要 hold、模型权重、GPU、API key 或已运行的业务服务。装有 ROS 和测试消息时，同一入口还会启动独立仿真器，验证三路相机、双臂发布和反馈。

默认运行行为是 **非空动作原样透传，空输入保持实测姿态**。默认 `sync / qpos`、一帧、零延迟，不申请控制权、不写共享状态、不联网。非空的上游动作仍可能让机器人运动。

## 手动运行

```bash
./setup.sh --policies example
# 如需启动时就只加载 example，在本机 configs/rc.mock.yaml 设 startup_policy: example。
./scripts/compose.sh up -d
./scripts/compose.sh attach robot-control
```

`setup` 会准备 example 的 Python 3.14 依赖镜像和 [example_chain 的清单](../example_chain/policy.yaml.example)，不会为链另建镜像。已有本地配置会保留；从旧版升级时核对 [模板](policy.yaml.example) 的新命令与参数。

```text
help policy example
policy switch example
monitor example
resume
policy example example
```

`policy switch` 成功后是暂停状态。`policy example example` 返回该实例的快照，包括推理/观测次数、配置版本、控制代次、消费数量和图像摘要。`monitor example`、`camera`、`monitor` 是本地 TUI 面板切换命令；`monitor()` 仅读取有锁的标量快照，不做图像、网络或控制 I/O。

## 可以验证什么

| 能力 | 在 mock RC 中操作 | 可以看到的结果 |
| --- | --- | --- |
| 动作与调度 | `policy example config set execution.control_mode mixed`、`policy example config set params.chunk_size 4`、`policy example config set execution.mode async`，再 `resume` | 控制模式与帧数在原进程重新初始化；调度模式普通应用。空输入时 qpos 读 `state["qpos"]`，epos 读 `state["eef"]`，mixed 从 qpos 开始逐帧交替；非空时保留上游动作与标签。 |
| 模拟慢推理 | `policy example config set params.inference_delay_s 0.18` | 仅阻塞推理回调。sync 等结果再消费；async 可继续发送缓存中已就绪的目标，结果返回后裁掉实际消费的前缀。 |
| 配置与日志 | `policy example config set params.message "新消息"`、`policy example config set params.log_every_n_calls 2`、`policy example example` | 普通应用保留 worker、context 与配置引用。重复节点用 `policy example-1 config set params.message "新消息"` 只修改指定实例。原 custom 命令 `policy example set-message "新消息"` 保留原效果。 |
| 配置后重置 | `policy example config set params.reset_token 1` | handler 只修改候选配置并登记 reset；返回后才清空目标实例的统计与待发结果，保留 context。 |
| 主动暂停与结束 | 先 `resume`，再 `policy example next-result pause 人工检查`、`policy example next-result completed 成功` 或 `policy example next-result terminated 未完成`（每次分别运行） | 下一轮推理提交一次请求。整条链成功后由 RC 丢弃结束轮动作、清空缓存并暂停。reset_context 会清除旧请求。 |
| 异常与恢复 | 在 mock 上运行中执行 `policy example next-result fault 演练`，观察 `status`，再 `policy example retry` | 故障走 RC 原有清理流程；MCP 已持有的控制权不会被 policy 抢回。 |
| 全局状态 | `policy example shared-read`，再 `policy example shared-write "新值" <刚读到的版本>` | 只更新共享键 `example_note`。其他实例可读，过期版本报冲突且不修改。 |
| 录制与快照 | `record start`、`resume`、`snapshot`、`record stop` | 录制目录有反馈、实际发送的动作、policy 追踪与图片；snapshot 生成三张原图和 state.json。 |
| 冷重载与保存 | `policy example set-chunk-size 6`，再 `policy example config save` | 原 custom handler 返回冷字段补丁，RC 重建目标实例并暂停整链。显式保存才更新来源 YAML。 |

## 策略自行决定配置效果

`policy <实例> config set <key> <value>` 将 key 与原始 value 文本交给该策略的 `configure(context, config, key, value, callback)`。`config` 是独立候选副本；handler 不修改运行中的 context、模型或服务。处理完后不调用 callback 表示普通应用，`callback("reset")` 请求重置，`callback("reinitialize")` 请求同进程重新初始化。动作在 handler 返回后执行。

内置策略使用完整的 `params.xxx` 与 `execution.xxx` 名称。值支持 JSON（例如 `null`、`4`、`"带空格的文本"`），字符串也可用单引号或不加引号的整段文字。若字符串看起来是 JSON 数字、布尔或 null，请加引号保留字符串类型。未知 key、不可变元数据以及业务不允许的值会被拒绝。新配置入口不使用 `hot_reloadable` 白名单；旧 custom 命令仍遵守其声明的效果。

| 来源 | 配置 key | 后续效果与业务限制 |
| --- | --- | --- |
| example | `params.message`、`params.log_every_n_calls`、`params.inference_delay_s` | 普通应用；日志周期为正整数，延迟为 0 到 5 秒。 |
| example | `params.reset_token` | 非负整数，登记 reset，清空统计及待发结果。 |
| example | `params.chunk_size`、`params.reader_url` | reinitialize；帧数为 1 到 256。 |
| hold | `params.chunk_size` | 普通应用；正整数。 |
| qwen35_single / qwen35_twostage / qwen35_history | `params.task` | reset；非空文本或 `null`（恢复模型配置中的默认任务），清除旧任务的动作/历史。 |
| qwen35_single / qwen35_twostage / qwen35_history / pi05 / internvla_maas | `params.model_config_path` | 先只读验证对应模型配置，再 reinitialize；不在 handler 中加载模型。 |
| llm_direction / llm_failure | 已有 `params` 字段 | reinitialize，因为运行时保留独立参数副本；供应商路径先验证配置文件。failure 的 task/prompt 属于供应商配置，不能作为 params 修改。 |
| 所有可执行来源 | `execution.mode`、`execution.call_every_actions` | 普通应用；模式为 sync/async，周期为正整数。Qwen 与 pi05 仅允许 sync 和 1。 |
| 所有可执行来源 | `execution.control_mode`、`execution.tui_command_workers` | reinitialize；workers 为正整数。example/failure 支持 qpos、epos、mixed；direction 仅支持 epos；其他来源仅支持 qpos。 |

用 `policy <实例> config show` 查看已提交配置，`policy <实例> config save` 写回来源目录。重初始化失败不会自动冷重载，需手动执行 `policy <实例> retry`。

`policy()` 和 `observe()` 只在各自回调内读取只读 NumPy 图像；摘要中的 shape、时间戳和首像素均已脱离映射。回调结束不保存图像、视图或推理 runtime。观测、命令、monitor 可以与推理并发；只有 RC 控制线程发送动作并消费缓存。

## 原生链和 MCP 交还

```text
policy switch example_chain
policy example-0 example
policy example-1 example
monitor example-0
policy example-1 reinitialize
policy example-1 config set params.chunk_size 3
policy example-1 set-chunk-size 3
policy example-1 config save
```

同一来源在链里分别是 `example-0` 和 `example-1`，每个实例有独立容器、host、执行进程和 context。`policy example-1 set-chunk-size 3` 只重建第二个实例。`policy example-1 set-chunk-size-all 3` 使用相同 handler 并重建两个容器。两种命令都只修改第二个实例的参数。`policy example-1 config save` 把选中实例的配置写回来源 `policy/example/policy.yaml`；两个实例并存时不能用含糊的 `policy example config save`。在 TUI 中也可用 `policy manage` 编辑、保存和应用同样的链。

`policy example-1 reinitialize` 使用当前配置，`policy example-1 config set params.chunk_size 3` 使用策略验证后的候选配置。用 `policy example-1 example` 比较执行前后的 `module_id` 与 `context_id`，可以看到同一模块中创建了新 context。两条命令结束后均等待 `resume`。

在启用 MCP 的 mock RC 中可验证一次完整的交接：

```text
mcp take
mcp return_and_wait {"timeout_s":0}
status
policy example-1 next-result completed "第二个实例完成"
mcp get_policy_result
```

接管成功后再归还；等 `status` 显示 `running` 再提交实例命令。查询结果含 `outcome=completed`、消息和来源 `example-1 / cell 2`，控制权交还 MCP。普通 pause 会取消等待；无等待任务的 finish 只暂停。需要直接操控时，先 `mcp take` 并确认成功，再使用 `mcp send_command` 或 `mcp move_eef`。策略命令没有自行开放给 MCP。

## 外部图像读者（可选）

[外部 reader 客户端](remote_reader.py) 通过 `params.reader_url` 配置并重新初始化后启用，只向能共享同一 POSIX IPC 的演示服务传描述符。默认空地址不会发请求。example 先获取服务实例及代次、注册 owner，再为每次读取单独保留租约；只有 instance、owner、request 全部匹配且明确确认释放后才调用 `release_images`。断连或错误回执会报错，不能在 finally 里猜测读者已退出。

默认套件会自行启动并清理[本地参考读者](../../tests/example_reader.py)，实际映射帧并验证错误回执不会回收该帧。手动接入时，RC 和策略都必须能访问同一 reader URL（各容器的 localhost 不互通）。

## 真实容器验收

```bash
./setup.sh --policies example
python3 tests/example_docker_smoke.py --evidence-dir /tmp/example-evidence
```

脚本使用独立临时部署和 mock 机器人，只准备 RC、IPC anchor 和 example，不读取本机策略配置。它检查容器身份、热改不换容器、局部与整组两种冷重载的进程及容器身份、实例保存、退出释放，并留下 result.json、container.log、cleanup.json。日常测试范围及公共接口变更的升级规则见[验证文档](../../docs/verification.md#按改动选择测试)。

手动部署使用完后在 RC 输入 `exit`，再运行 `./scripts/compose.sh down`。mock、容器和 ROS 仿真验收均不代替真机或真实模型验收。
