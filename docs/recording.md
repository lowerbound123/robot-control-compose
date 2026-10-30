# 连续录制与推理追踪

在 RC 命令行使用 `record on` 开始录制，`record off` 停止并等待已接收数据落盘，`record status` 查看状态。裸 `record` 切换录制开关。

```text
record on
record status
pause
record status
record off
```

录制以独立的 30 Hz 时钟读取机器人缓存，和控制循环频率、policy 推理频率、终端相机预览刷新频率分开。暂停、MCP 接管、没有活动 policy 时仍可采样。录制命令本身不会恢复运动、复位机器人、切换策略或改变控制归属。录制开关只接受本地命令；MCP 即使已经接管控制权，也不能执行 `record on/off`，只能从已有 `status` 的 `recording` 字段读取状态。

路径位于配置的 `outputs_directory/logs/`，通常是项目的 `outputs/logs/`。每次开始录制都创建一个新的 `record_日期_时间_唯一标识/`，不会覆盖前一段。日期按 UTC+8 命名，JSONL 和 CSV 保留墙钟及单调时钟的数值时间。完整路径以 `record status` 的 `path` 为准。

## 文件与对齐规则

| 文件 | 内容 |
| --- | --- |
| `rc_<run_id>.jsonl` | RC 本轮运行的追加日志，位于录制目录的上一级；不开启录制时也持续写入 |
| `samples.jsonl` | 30 Hz 采样提交记录、时间、每路 PNG 路径／帧身份、重复或缺失标记，以及本区间采用的 `control_events` 发布引用 |
| `state.jsonl` | 完整机器人缓存状态及 `state_error`；ACOne 包含原始反馈字段 |
| `qpos.csv`、`epos.csv` | 每个采样时刻的 14 维关节／末端状态；前 3 列是 `sample_id,timestamp_ns,monotonic_ns` |
| `control_qpos.csv`、`control_epos.csv` | 该采样区间各 arm **最后一次成功发布**的目标；另一模式、没有发送或未成功的 arm 留空 |
| `control.jsonl` | 每次动作尝试及逐 arm 成功发布，包含模式、7 维值、动作尝试关联和实际时间 |
| `policy.jsonl` | 观察输入、每实例请求与结果、错误／超时／取消、原生链各环节及整体事件 |
| `events.jsonl` | 录制期间的 RC/policy 日志 |
| `llm_failure.jsonl` | 从策略结构化日志中提取的失败检测请求与结果收取事件 |
| `session.jsonl` | 开始、停止、错误、诊断缺口及停止时未完成请求 |
| `configs/` | 开始录制时和后续记录的配置快照，敏感字段脱敏 |
| `images/<camera>/<sample_id>.png` | 每次有效采样的完整 RGB 无损 PNG |
| `assets/<camera>/<sha256>.png` | 按图像内容寻址的 PNG；相同像素通过硬链接复用，`sha256` 同时覆盖形状和 dtype |

正常停止会话的四份 CSV 行数和 `sample_id` 与 `samples.jsonl` 一致。提交记录最后写入；PNG、状态与四份 CSV 先 flush/fsync，再写入 `sample_commit`。异常退出可能留下额外的 CSV 尾行或孤立 PNG，离线读取应以 `sample_commit` 的有效前缀为准。末尾缺少 `record.stopped` 或存在错误标记的会话不算完整成功。普通运行日志定期刷新，正常停止时统一完成 flush/fsync。

`epos` 是框架已有的 14 维末端状态：左右 arm 各 6 个位姿坐标加 1 个 gripper 值。CSV 不补写上次动作：没有真实发布的新目标时留空，避免把持续保持误认为又发送了一次。左 arm 成功、右 arm 发布失败时，仅左 arm 有成功记录。

成功发布之后，观察回调偶尔会受线程调度影响而晚于当前采样边界入队。这次真实发送会保留原始 `monotonic_ns`，进入下一次实际收取它的采样行，并标记 `late_observer`。对齐控制 CSV 时应使用 `samples.jsonl` 的 `control_events` 发布引用，不应仅按单调时间窗口重新分配行。

相机帧保留原始帧序号、传感器墙钟时间和接收单调时间。轮询相同帧会明确标记 `repeated`；相机超时或解码错误不会伪造新图像。缓存中的旧图像保留失效原因，录制提交将无有效图像的相机标记为 `missing`。采样线程错过截止时间则产生 `missed` 记录，不能把这类行当成有效传感器数据。

`state_error` 非空时，状态 CSV 仍可能保留最后一份完整反馈缓存；数值存在不代表该时刻又收到了新反馈。判断反馈新鲜度应同时读取 `state.jsonl` 的错误、序号与原始时间，而不能仅依据 CSV 行的 30 Hz 采样时间。

## 推理、链和失败检测

`policy.observation` 保存与本次实际 image lease 对应的 RGB，并使用同一 `observation_id` 关联 policy 输入。image pool 暂时不能发布新帧时，记录应对应 policy 实际拿到的帧，不能将新的相机缓存冒充为该次输入。policy 请求事件只保存共享内存描述符；离线读取应使用 `image_files` 指向的 PNG，不能依赖运行结束后的 SHM 名称。

每个可执行实例分别产生 `policy.start/result/error/cancel/timeout`。原生链的子请求携带 `chain_id`、`chain_request_id`、`cell`，同时保留实例自己的 `request_id`、来源目录身份、代次及配置版本；`a-0` 与 `a-1` 的输出不会合并。整体链事件还记录 `policy.stage_start/stage_result/stage_error`。迟到或过期响应分别记录为 `policy.late/stale`，记录行为不会使它们重新进入控制。

录制可能在推理中途开始或结束。只有结果、没有本段开始事件的请求带 `start_outside_recording`；停止时仍未结束的请求出现在 `session.jsonl` 的 `unfinished_requests`。这些标记表示录制边界，不等同于 policy 故障。

`llm_failure` 使用 `query_id` 关联请求执行和结果收取。日志包括提交／开始／结束／收取时间、HTTP 耗时、使用量、finish reason、解析后的 `failed`、原始响应和采纳／丢弃原因。请求完成与结果被控制逻辑收取是两个不同的时刻。单条结构化诊断有 8000 字节上限；截断时保留原始大小和截断字段标记。密钥与凭据经过脱敏，图片数据不进入诊断行。

## 状态、容量和退出

`record status` 返回 `state`、`session_id`、`path`、`error`、`samples_written`、`samples_accepted`、`repeated_frames`、`missing_frames`、`missed_samples`、`extra_dropped`、`pending_bytes`、`pending_samples` 和 `peak_pending_bytes`。

图像编码、序列化和磁盘写入由独立 writer 进程执行。默认采样内存额度为 512 MiB，附加诊断额度为 64 MiB，最多 60 个待处理采样。在途字节估算覆盖保留图像、IPC 和编码开销，因此不等于进程 RSS 或磁盘文件大小。附加诊断拥塞时记录丢弃数量；必需采样超过额度会停止该段录制并给出错误，保留已写数据。磁盘写入失败、writer 退出或停止排空超时也会形成明确错误。

退出走 RC 的统一绝对期限清理。被迫终止 writer 属于强制清理，RC 返回非零；不能把只有目录存在、只有部分 PNG 或仍有 pending 数据当作成功停止。

## 可复现验收和压测

`tests/manual/recording_acceptance.py` 使用真实 `RecordingService`、SDK host/worker、POSIX 共享内存和 loopback HTTP 模型。默认以三路持续变化的 640×480 RGB、30 Hz 相机／录制、50 Hz 控制发送运行 60 秒。它逐个核对采样提交和 CSV，以逐 arm 发布事件重算控制 CSV，并对每个唯一 PNG 做无损像素哈希验证；每次模型实际收到的三路像素还会与保存的观察输入比较。

使用已有 RC 测试镜像启动隔离验收：

```bash
python3 tests/manual/recording_acceptance.py \
  --docker-image robot-control-compose-chain-tests:local \
  --duration 60 --width 640 --height 480 \
  --evidence /tmp/rc-recording-acceptance-640x480
```

也可使用自己的 `docker/rc.Dockerfile --target test` 镜像替换名称。脚本不会拉取或构建镜像；它复制本轮源码，挂载为只读，使用唯一 `io.robot-control.deployment` 标签、关闭外部网络，不挂载 Docker socket，完成后只清理本轮容器。证据目录必须是新目录。

已安装本项目依赖的测试解释器可直接运行：

```bash
PYTHONPATH="$PWD/src:$PWD/packages/policy-sdk/src" python3 \
  tests/manual/recording_acceptance.py \
  --duration 60 --width 1280 --height 720 \
  --evidence /tmp/rc-recording-acceptance-1280x720
```

可调整 `--control-hz`、`--model-interval`、`--model-delay`、`--sample-budget-mib`。`--pattern moving` 使用可压缩的变化图像；`--pattern noise` 每帧生成独立噪声，增加编码和磁盘压力，文件量也会明显增大。改变分辨率或图像内容后应重新验收，不能把低分辨率结果推断为任意相机吞吐。

`--smoke --duration 3` 仅用于短时检查，报告明确标为 smoke，不能替代至少 60 秒的连续验收。默认要求零采样截止时间缺口，各路实际有效写入至少 29 Hz；如显式调整 `--max-gap-ratio`，报告会保留实际缺口，不能把容许阈值当成零缺口。

真实 ROS 仿真使用同一脚本的 `--ros` 分支：

```bash
python3 tests/manual/recording_acceptance.py \
  --docker-image robot-control-compose-chain-tests:local \
  --ros --smoke --duration 10 --width 64 --height 48 \
  --evidence /tmp/rc-recording-ros-smoke
```

这条命令启动 `ACOneRobot` 和已有 `robot_control_test_sim` 的独立进程，使用真实 ROS 2 消息、压缩图像订阅和命令发布。仿真反馈为 90 Hz，相机每三个反馈周期发布一次，即 30 Hz。除了录制文件对齐，还逐 arm 核对仿真端实际收到的模式和 7 维目标；回执与 simulator 日志保留在证据目录。ROS namespace、domain 和容器部署标签均独立；不启动厂商驱动。ROS 模式使用固定合成 RGB 图像，动态编码压力由前述 Mock 压测覆盖。

证据包括 `report.json`、`status-history.json`、`model-requests.json`、policy host 日志和完整录制文件；Docker 运行另存 `deployment.json`、`teardown.json`、`container.log` 及源码快照。报告给出实际采样／控制频率、各路缺失和重复、源帧序号跳跃、队列字节峰值、轮询观察到的队列采样峰值、动作发送及发送前记录总耗时的 p50/p95/p99/max，以及控制启动迟到量。`teardown.json` 按本轮唯一部署标签核对容器是否全部退出；发生强制容器移除时验收命令返回非零。

`tests/test_recording_signal.py` 另外覆盖真实 RC 子进程收到 SIGTERM 时的录制收尾：正在录制的 session 需要正常停止、CSV 已写入、writer 进程和 recorder 线程均退出。该测试使用策略替身验证 RC 的信号清理路径；上述手动脚本验证独立 recorder 的持续吞吐和实际 SDK/HTTP/ROS 传输，二者证据范围不同。

本地 HTTP 替身只验证传输、关联与记录完整性，不代表真实模型质量或付费供应商可用性；Mock 和 ROS 仿真证据也不能替代真实相机、机器人动作和现场安全验收。

## 2026-10-30 独立验收结果

以下两轮使用最终源码快照，在本机 OrbStack Linux/arm64、ROS 2 Humble / Python 3.10.12 测试镜像中完成。运行期间主机和 Linux 全套测试均暂停，两个验收容器顺序执行；源码哈希、镜像 ID 和唯一部署标签均保存在各自证据目录中。

| 指标 | Mock 连续压测 | 真实 ROS 仿真短测 |
| --- | --- | --- |
| 时长、分辨率 | 60.002473 s；三路 640×480 动态 RGB | 10.007046 s；三路 64×48 固定合成 RGB |
| 采样与各路有效 PNG | 1801；30.015430 Hz | 301；30.078807 Hz |
| 采样截止缺口／缺失帧／附加事件丢弃 | 0 / 0 / 0 | 0 / 0 / 0 |
| 各路重复帧／源序号跳跃 | 每路 3 / 3 | head 3 / 3，left_wrist 4 / 4，right_wrist 5 / 5 |
| 控制周期及实际频率 | 2999；49.981273 Hz | 500；49.964796 Hz |
| 逐 arm 发布 | 5998，全部与提交引用及 CSV 一致 | 1000，另与 simulator 的 1000 条实际接收回执逐项一致 |
| 本地 HTTP / SDK 请求结果 | 59 对，保存的输入像素全部一致 | 10 对，保存的输入像素全部一致 |
| 唯一采样 PNG 像素核验 | 5394 个 | 3 个；固定像素经硬链接复用 |
| 动作发送耗时 p99 / max | 0.591678 / 3.159562 ms | 2.724691 / 15.702186 ms |
| 包含尝试记录的发送总耗时 p99 / max | 1.605949 / 5.437733 ms | 3.900286 / 15.935230 ms |
| 控制启动迟到量 max | 35.746738 ms | 18.768860 ms |
| 在途字节估算峰值 | 149797208 bytes | 2590160 bytes |
| 轮询观察到的待写采样峰值 | 8 | 7 |
| late observer／强制清理／残留容器 | 0 / 无 / 0 | 0 / 无 / 0 |

完整证据在 `/private/tmp/rc-record-20261030/manual-mock60-isolated/` 与 `/private/tmp/rc-record-20261030/manual-ros10-isolated/`。两轮均返回 0，正常结束后 pending bytes/samples 清零。控制启动迟到量与发布函数耗时单独报告，不能据此声称普通宿主调度具有硬实时保证。

另保留了 `manual-mock60-frozen/` 的并发压力失败证据：当时主机全套测试同时运行，60.002761 秒内出现 1 个采样截止缺口、每路 1 个缺失帧，其余文件／像素／动作对应检查通过。该轮因默认零缺口门槛返回失败，没有调整门槛将其改记为通过；最终独立复测结果如上。`manual-mock60-final/` 是等待 writer 冻结时主动取消的中间轮次，不计入验收。
