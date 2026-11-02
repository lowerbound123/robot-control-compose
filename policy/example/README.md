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

`setup` 会准备 example 的 Python 3.14 依赖镜像和 [example_chain 的清单](../example_chain/policy.yaml.example)，不会为链另建镜像。已有本地配置会保留；从旧版升级时核对 [模板](policy.yaml.example) 的新命令、参数及 `hot_reloadable` 列表。

```text
help example
switch example
monitor-example on
resume
example
```

`switch` 成功后是暂停状态。`example` 和 `policy example example` 都返回该实例的快照，包括推理/观测次数、配置版本、控制代次、消费数量和图像摘要。`monitor-example`、`camera`、`monitor` 是本地 TUI 面板；`monitor()` 仅读取有锁的标量快照，不做图像、网络或控制 I/O。

## 可以验证什么

| 能力 | 在 mock RC 中操作 | 可以看到的结果 |
| --- | --- | --- |
| 动作与调度 | 独立运行时用 `set policy.execution.control_mode "mixed"`、`set-chunk-size 4`、`set policy.execution.mode "async"`，再 `resume` | 三项均冷改。空输入时 qpos 读 `state["qpos"]`，epos 读 `state["eef"]`，mixed 从 qpos 开始逐帧交替，动作与模式一一对应；非空时保留上游的帧和标签。 |
| 模拟慢推理 | `set policy.params.inference_delay_s 0.18` | 仅阻塞推理回调。sync 等结果再消费；async 可继续发送缓存中已就绪的目标，结果返回后裁掉实际消费的前缀。默认延迟为 0。 |
| 热配置与日志 | `set policy.params.message "新消息"`、`set policy.params.log_every_n_calls 2`、`example` | worker 和配置引用保持不变；两端确认版本后命令、monitor 及后续日志反映新值。只有 manifest 的白名单叶字段可热改。 |
| 主动暂停与结束 | 先 `resume`，再 `next-result pause 人工检查`、`next-result completed 成功` 或 `next-result terminated 未完成`（每次分别运行） | 命令只登记一次请求。下一轮 `policy()` 调用 `runtime.pause/finish` 并返回合法动作；整条链成功后由 RC 丢弃结束轮动作、清空缓存并暂停。reset_context（含 resume/归还时的重置）会清除旧请求。 |
| 异常与恢复 | 在 mock 上运行中执行 `next-result fault 演练`，观察 `status`，再 `switch example` | 真实推理异常走 RC 原有故障/清理流程；MCP 已持有的控制权不会被 policy 抢回。 |
| 全局状态 | `shared-read`，再 `shared-write "新值" <刚读到的版本>`；用 `show global example_note` 查看 | 只更新共享键 `example_note`。其他实例可读，过期版本报冲突且不修改；`unset global.example_note` 可清除，退出按 RC 规则持久化。 |
| 录制、状态与快照 | `record on`、`resume`、`snapshot`、`record off`；`save demo_pose`、`reset`、`load demo_pose` | 录制目录有反馈、实际发送的动作、policy 追踪与图片；snapshot 生成三张原图和 state.json。保存/加载姿态通过 RC 控制线程。 |
| 冷重载与保存 | `set-chunk-size 6`，再 `save-config example` | handler 只返回已有冷字段的补丁。RC 重建目标实例并暂停整链。显式保存才更新来源 YAML。 |

`policy()` 和 `observe()` 只在各自回调内读取只读 NumPy 图像；摘要中的 shape、时间戳和首像素均已脱离映射。回调结束不保存图像、视图或推理 runtime。观测、命令、monitor 可以与推理并发；只有 RC 控制线程发送动作并消费缓存。

## 原生链和 MCP 交还

```text
switch example_chain
example-0-example
policy example-1 example
monitor-example-0 on
example-1-reinitialize
example-1-reinitialize-chunk-size 3
example-1-set-chunk-size 3
save-config example-1
```

同一来源在链里分别是 `example-0` 和 `example-1`，每个实例有独立容器、host、执行进程和 context。`example-1-set-chunk-size 3` 只重建第二个实例。`example-1-set-chunk-size-all 3` 使用相同 handler 并重建两个容器。两种命令都只修改第二个实例的参数。`save-config example-1` 把选中实例的配置写回来源 `policy/example/policy.yaml`；两个实例并存时不能用含糊的 `save-config example`。在 TUI 中也可用 `manage-policy` 编辑、保存和应用同样的链。

`example-1-reinitialize` 使用当前配置，`example-1-reinitialize-chunk-size 3` 使用冷字段补丁。用 `example-1-example` 比较执行前后的 `module_id` 与 `context_id`，可以看到同一模块中创建了新 context。两条命令结束后均等待 `resume`。

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

[外部 reader 客户端](remote_reader.py) 通过 `params.reader_url` 冷配置启用，只向能共享同一 POSIX IPC 的演示服务传描述符。默认空地址不会发请求。example 先获取服务实例及代次、注册 owner，再为每次读取单独保留租约；只有 instance、owner、request 全部匹配且明确确认释放后才调用 `release_images`。断连或错误回执会报错，不能在 finally 里猜测读者已退出。

默认套件会自行启动并清理[本地参考读者](../../tests/example_reader.py)，实际映射帧并验证错误回执不会回收该帧。手动接入时，RC 和策略都必须能访问同一 reader URL（各容器的 localhost 不互通）。

## 真实容器验收

```bash
./setup.sh --policies example
python3 tests/example_docker_smoke.py --evidence-dir /tmp/example-evidence
```

脚本使用独立临时部署和 mock 机器人，只准备 RC、IPC anchor 和 example，不读取本机策略配置。它检查容器身份、热改不换容器、局部与整组两种冷重载的进程及容器身份、实例保存、退出释放，并留下 result.json、container.log、cleanup.json。日常测试范围及公共接口变更的升级规则见[验证文档](../../docs/verification.md#按改动选择测试)。

手动部署使用完后在 RC 输入 `exit`，再运行 `./scripts/compose.sh down`。mock、容器和 ROS 仿真验收均不代替真机或真实模型验收。
