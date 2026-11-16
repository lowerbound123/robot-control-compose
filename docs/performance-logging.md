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

真机须先同步本次 RC、两个 SDK 及策略源码。仓库默认 Compose 将这些源码挂载进容器，本次没有新增依赖；按原部署方式统一重建运行容器以加载新模块。例如原来使用 ACOne 覆盖文件时，在仓库根目录运行：

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
- 对应 `record_*/events.jsonl`：录制期间写入的同类事件。
- 整个 `record_*` 目录：同时保留 `samples.jsonl`、`control.jsonl`、`policy.jsonl`、CSV、配置快照和会话状态，以便核对空白帧。

实际日志根目录以当前配置的 `outputs_directory/logs` 为准。请把 **本轮 `rc_<run_id>.jsonl` 和相关 `record_*` 目录一起打包**。只提供录制目录可能遗漏录制开始前、停止后，以及后台诊断尚未排空时的事件。

## 字段与关联

顶层 `rc_run_id` 区分运行；`trace_id` 区分一次请求的诊断链。按事件类型提供 `instance_id`、`source_id`、`request_id`、`generation`、`config_version`、`observation_id`、`attempt_id`、`connector_request_id` 或 `sample_id`。重复策略实例仍用各自实例名，不能按来源目录合并。

`timestamp_ns` 是墙钟时间；`monotonic_ns` 和所有 `*_ns` 是纳秒，除以 `1_000_000` 得毫秒。`performance.points` 保存逐阶段时间、进程 ID 和线程名；`duration_ns` 表示对应段的实测耗时，`wait_ns` / `lock_wait_ns` 表示锁等待。进程 ID 在不同容器中可能重复，需结合实例和阶段看。跨进程时间差适用于本项目同一主机、共享内核时钟的容器部署。

`logged_monotonic_ns` 是后台诊断线程开始转交日志的时刻，不是业务动作时间。分析时使用业务点的 `monotonic_ns`；后台落盘顺序不代表跨线程因果顺序。

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
| `record.enqueue` | 原有录制入队的锁等待、数据大小估算、深复制、可选图像复制和总处理时间 |
| `record.sample` | 计划/实际采样时刻、采样迟到、缓存读取耗时、missed、逐臂发布引用与 late 标志 |

`worker.policy.duration_ns` 才是 SDK 调用策略函数的耗时；它与原有 `policy.result.elapsed_s` 的跨进程往返口径不同。`worker.normalize` 单列返回动作校验耗时，`worker.images_mapped` / `worker.images_released` 标记共享图像映射与释放边界。

两个 history 策略还记录 `history.settings_lock`、`history.lock_acquired`、`history.update`、`history.snapshot`、`history.cache` 和 `history.model`。这可以区分缓存返回、历史图像处理、观察线程锁竞争和模型等待。观察回调使用 `history.observe_lock` / `history.observe_update`。

`rc.result_trace_begin/end` 区分原有结果日志处理，`rc.result_available` / `chain.result_available` 表示已入本地结果队列、仍可能处于该组件锁内，`rc.policy_receive_complete` 标记接收处理退出锁之后，`rc.result_polled` / `rc.chain_result_collected` 标记控制侧实际收取，`rc.result_installed` 标记缓存安装完成。**原有结果日志时间不等于 RC 已经安装该结果。**

传输点之间的差值包含该段序列化、队列、socket 和线程调度，不能仅凭这个差值全部归因于网络或函数计算。诊断不主动唤醒策略链或改变控制线程原有轮询节拍。

## 容量和数据完整性

新增诊断队列最多 2048 条，单次传输诊断最多 128 个时间点。饱和时丢弃可选诊断，后续事件的 `dropped_events` 或 `diagnostic_gap` 报告缺口；时间点超限用 `performance.dropped_points` 报告。原有 recorder 自己的 `diagnostic_gap` 仍需一并检查。有缺口时不能用“找不到事件”证明该环节没有执行。

性能事件不会再次触发 `record.enqueue` 性能事件，避免递归放大。新增日志的慢写入和输出异常不阻塞请求提交，不更改动作结果；原有业务 trace / 录制入队的等待保持原样，并由新时间点测量。

旧 SDK 不返回性能元数据时，RC 记录 `rc.result_missing_worker_timings`；链成员缺失时记录 `chain.child_missing_timings`。这表示覆盖不完整，不代表动作失败。真机测量前应同步全部相关源码。
