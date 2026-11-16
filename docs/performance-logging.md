# DEBUG 全链路性能日志

默认 `logging.level: INFO` 不生成性能日志，也不向策略或 connector 请求添加性能元数据。将现有 RC 冷参数改为 `DEBUG` 后，记录控制线程、观察输入、策略通信与回调、动作发布、回执收取和录制采样的时间。`TRACE` 也包含这些诊断，但诊断条目的级别仍为 `debug`。

这项功能只测量，不改变发送频率、同步等待、动作消费、控制权、策略生命周期或图像租约。记录时间、数量、结果类别和关联 ID，不记录图像像素、动作数组、任务文本或配置内容。

## 开启和关闭

在当前 RC 的 TUI 中输入，保留字符串引号：

```text
config rc set logging.level "DEBUG"
config rc save
exit
```

这是冷参数：`set` 只修改草稿，`save` 保存，**重启后才启用**。也可以在停止部署后编辑实际使用的 RC 配置，例如 ACOne 的 `configs/rc.acone.yaml`：

```yaml
logging:
  level: DEBUG
```

真机须先同步对应版本的 RC、两个 SDK 及策略源码。仓库默认 Compose 将这些源码挂载进容器。本轮录制隔离、结果通知与周期 hold 更新没有新增依赖或修改镜像，**不需要重新构建镜像**；需要按原部署方式停止并重新启动运行容器以加载新模块。例如原来使用 ACOne 覆盖文件时，在仓库根目录运行：

```bash
./scripts/down.sh
./scripts/up.sh compose.acone.yaml
./scripts/attach.sh
```

若原部署使用其他覆盖文件，保留原文件组合。不要只修改已运行进程的文件后继续采样。用 `config rc show` 确认 `active.logging.level` 为 `DEBUG`。

复现时开启一段录制：

```text
record start
```

按原有步骤运行任务、复现需要调查的问题，然后停止录制：

```text
record stop
```

已有自动录制时可以继续使用。首次采集建议覆盖几次出现空白的动作区间；DEBUG 数据量和额外开销高于 INFO。结束后正常退出，等待诊断队列及录制写入排空。

恢复默认：

```text
config rc set logging.level "INFO"
config rc save
exit
```

随后按原部署方式重启。

## 收集哪些文件

性能事件使用 `event: "performance"`、`level: "debug"`，通过有界队列后台进入现有日志文件，不逐条刷新 TUI：

- `outputs/logs/rc_<run_id>.jsonl`：本次运行完整诊断；未开启录制也会写入。
- 对应 `record_*/events.jsonl`：录制期间入口接收的同类事件，后台处理延迟不会将它们移到下一段录制。
- 整个 `record_*` 目录：同时保留 `samples.jsonl`、`control.jsonl`、`policy.jsonl`、CSV、配置快照和会话状态，以便核对空白帧。

实际日志根目录以当前配置的 `outputs_directory/logs` 为准。请把 **本轮 `rc_<run_id>.jsonl` 和相关 `record_*` 目录一起打包**。只提供录制目录会遗漏录制开始前、停止后接收的事件；正常 `record stop` 会等待本段已接收的诊断及录制数据排空。

## 字段与关联

顶层 `rc_run_id` 区分运行；`trace_id` 区分一次请求的诊断链。按事件类型提供 `instance_id`、`source_id`、`request_id`、`generation`、`config_version`、`observation_id`、`attempt_id`、`connector_request_id` 或 `sample_id`。重复策略实例仍用各自实例名，不能按来源目录合并。

`timestamp_ns` 是墙钟时间；`monotonic_ns` 和所有 `*_ns` 是纳秒，除以 `1_000_000` 得毫秒。`performance.points` 保存逐阶段时间、进程 ID 和线程名；`duration_ns` 表示对应段的实测耗时，`wait_ns` / `lock_wait_ns` 表示锁等待。进程 ID 在不同容器中可能重复，需结合实例和阶段看。跨进程时间差适用于本项目同一主机、共享内核时钟的容器部署。

`logged_monotonic_ns` 是录制后台完成诊断格式化的时刻，不是业务动作时间或磁盘持久化完成时间。分析时使用业务点的 `monotonic_ns`；后台落盘顺序不代表跨线程因果顺序。

| 顶层 stage | 用途 |
| --- | --- |
| `rc.loop` | 控制线程醒来、取得控制锁、处理发布回执/策略结果、执行 tick、下一截止时间 |
| `rc.snapshot` | 状态和图像缓存读取、图像池发布、观察录制耗时 |
| `policy.submit` | RC 本地子实例提交，包括原有同步 trace 处理完成时间 |
| `policy.receive` | RC 接收线程取得策略锁、原有结果 trace 前后、结果入队与接收处理结束 |
| `rc.policy_result` | 从请求准备、host/runtime 转发、SDK 排队和回调，到 RC 收取、归一化并安装结果 |
| `rc.observe_result` | 独立观察回调的通信、执行与链收取；包含历史观察锁和更新耗时 |
| `rc.send` | 限速、原有 control.attempt 记录、动作提交；用 attempt ID 关联发布回执 |
| `connector.receipt` | connector 收到命令、FIFO 出队、驱动发布、回执返回、RC observer 交付前后 |
| `record.enqueue` | 生产线程上的轻量入口总耗时、入口短锁等待、必要元数据快照和额度估算 |
| `record.prepare` | 已接收记录等待后台整理的时间，以及后台登记到 writer 队列的耗时 |
| `record.sample` | 计划/实际采样时刻、采样迟到、缓存读取耗时、missed、逐臂发布引用与 late 标志 |

`worker.policy.duration_ns` 才是 SDK 调用策略函数的耗时；它与原有 `policy.result.elapsed_s` 的跨进程往返口径不同。`worker.normalize` 单列返回动作校验耗时，`worker.images_mapped` / `worker.images_released` 标记共享图像映射与释放边界。

录制隔离后的入口指标带 `timing_version: 2`，与旧版指标区别如下：

- `record.enqueue.duration_ns`：控制／接收线程实际执行入口的总时间；`lock_wait_ns` 只测入口短锁，已不包含录制后台锁。`copy_ns` 和 `estimate_ns` 在共享录制锁外执行，图像不进行像素深拷贝。录制未开启或关闭时提前拒绝的记录可能没有这些分项。
- `record.prepare.queue_wait_ns`：从入口接收到后台开始整理的等待；`prepare_ns`：整理及登记耗时，均发生在后台。用同一运行中的 `record_sequence` 与入口事件关联。
- `record.sample.collect_lock_wait_ns`：采样线程取得回执队列的短锁等待；`rc.command_observer_begin/end` 仍测真实回执回调的总耗时。

不要把 `queue_wait_ns` 加到控制线程耗时上，也不要把后台写得更晚当成控制变快。判断修复效果应同时检查入口耗时、策略结果可用时间、RC 收取时间和相邻真实发布时间。

两个 history 策略还记录 `history.settings_lock`、`history.lock_acquired`、`history.update`、`history.snapshot`、`history.cache` 和 `history.model`。这可以区分缓存返回、历史图像处理、观察线程锁竞争和模型等待。观察回调使用 `history.observe_lock` / `history.observe_update`。

`rc.result_trace_begin/end` 区分原有结果日志处理，`rc.result_available` / `chain.result_available` 表示已入本地结果队列、仍可能处于该组件锁内，`rc.policy_receive_complete` 标记接收处理退出锁之后，`rc.result_polled` / `rc.chain_result_collected` 标记控制侧实际收取，`rc.result_installed` 标记缓存安装完成。**原有结果日志时间不等于 RC 已经安装该结果。**

结果通知使用 `rc.policy_ready_notified`（子策略通知上层）和 `chain.ready_notified`（整链通知 RC）。正常结果由事件唤醒上层，不依赖链原有的 5 ms 轮询；超时仍按子请求和整链截止时间检查，另保留 50 ms 健康检查兜底。就绪事件采用先清除标记、再检查队列、最后等待的顺序，通知本身不执行控制逻辑。

`rc.send.is_hold`、`hold_reason` 与 `control.attempt` 对应。sync 在等待策略或原动作确认时发送 hold，async 继续发送缓存、耗尽后 hold；hold 不消费缓存。`rc.loop` 中的 `nominal_deadline_ns` 是名义节拍，`next_deadline_ns` 同时满足实际发送间隔，`skipped_periods` 表示本轮处理越过的周期数量。结果提前收取不会提前发送；超期跳过错过的 tick，不再无条件从处理结束时重新等待一整个周期。

传输点之间的差值包含该段序列化、队列、socket 和线程调度，不能仅凭这个差值全部归因于网络或函数计算。就绪通知和周期 hold 在 INFO／DEBUG 下都生效；诊断开关本身不改变发送行为。

## 容量和数据完整性

DEBUG 在途记录最多 2048 条，同时占用录制服务原有 64 MiB 附加诊断额度；单次传输诊断最多 128 个时间点。饱和时丢弃可选诊断，`record status.debug_dropped` 累计 DEBUG 丢弃量，`extra_dropped` 包含所有可选记录丢弃。会话内的 `session.jsonl` 或会话外的基础日志用 `diagnostic_gap.count/reasons` 报告缺口，例如 `debug_capacity`、`debug_format_failed`、`diagnostic_capacity`。时间点超限仍用 `performance.dropped_points` 报告；分析旧版本时也需检查 `dropped_events`。有缺口时不能用“找不到事件”证明该环节没有执行。

性能事件不会再次触发 `record.enqueue` 或 `record.prepare`，避免递归放大。RC 的性能日志直接进入有界录制入口，格式化由后台执行，入口不取得 writer／后台锁。普通录制、回执记录及状态查询也使用这个短入口。慢写入和诊断格式化失败不会使动作等待；必需采样或发布历史无法保全时，仅停止本段录制并报告错误。

## 真机对照复测

保持策略、控制频率、相机配置、自动录制设置和任务步骤相同，先用 INFO 录制一轮，再冷改为 DEBUG、重启后录制同样一轮。每轮正常停止录制、退出并分别保存基础日志和录制目录。

对照时优先看动作块内的真实发布间隔、`rc.loop`、`rc.command_observer_begin/end`、`rc.result_trace_begin/end` 和 `record.enqueue.duration_ns/lock_wait_ns`。另外检查 `ingress_pending`、`pending_bytes`、`debug_dropped`、`diagnostic_gap` 及 `record.stopped` 错误，区分控制延迟与后台积压。升级周期 hold 后应分别报告动作发布数、hold 数和真实动作消费速度；发布更连续不等于模型计算加速。网络与策略内部计算仍需按各自时间点分析。

旧 SDK 不返回性能元数据时，RC 记录 `rc.result_missing_worker_timings`；链成员缺失时记录 `chain.child_missing_timings`。这表示覆盖不完整，不代表动作失败。真机测量前应同步全部相关源码。

## 待优化项目（2026-11-17）

以下两项由用户明确留待后续，当前就绪通知／hold／调度更新不修改它们。后续讨论性能优化时应先提醒并复查这份清单：

1. **缓存分支的历史图像处理。** 真机记录中 `history.update` 中位约 1.17 ms。后续研究更新前置、重复格式转换和复制；必须保持历史帧与已消费动作数的对应关系，并保证新推理取得完整历史。
2. **本地消息编解码的重复工作。** 包括 JSON 解析后再次完整序列化校验、多次进程转发和中间对象构造。下一轮先拆分排队、解析、校验与分发耗时，再确定修改范围；保留完整协议校验和生命周期／租约语义。
