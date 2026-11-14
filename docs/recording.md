# 连续录制与推理追踪

connector热切换时自动结束旧会话、在新连接提交后开启独立会话，并记录连接代次和前后关联。用户的record start/stop意图优先，连接失败后的手动重试保留最新意图。见 [connector 热切换](connector-switching.md#录制边界)。

在 RC 命令行使用 `record start` 开始录制，`record stop` 停止并等待已接收数据落盘，`record status` 查看状态。裸 `record` 切换录制开关。

```text
record start
record status
pause
record status
record stop
```

录制以独立的 30 Hz 时钟读取机器人缓存，和控制循环频率、policy 推理频率、终端相机预览刷新频率分开。暂停、MCP 接管、没有活动 policy 时仍可采样。录制命令本身不会恢复运动、复位机器人、切换策略或改变控制归属。MCP 可调用 `record(action="on"|"off"|"toggle"|"status")`，也可通过 `command("record start/stop/status")` 或裸 `command("record")` 操作；录制是默认只读规则的明确例外，无需先 take，在策略生命周期忙碌时仍可使用，不取消 `return_and_wait` 任务。

`record` 工具默认查询状态，不新增操作回执。写操作默认 `wait=false`、`timeout_s=30`，通过 `operation_id` 查询结果；`off` 的 `succeeded` 表示已接收数据完成落盘。等待超时和客户端断连只结束等待，不取消开始/停止操作。重复 `on/off` 沿用现有幂等行为，`toggle` 按录制服务队列顺序执行，不自动重试。

路径位于配置的 `outputs_directory/logs/`，通常是项目的 `outputs/logs/`。每次开始录制都创建一个新的 `record_日期_时间_唯一标识/`，不会覆盖前一段。日期按 UTC+8 命名，JSONL 和 CSV 保留墙钟及单调时钟的数值时间。完整路径以 `record status` 的 `path` 为准。

同一次 RC 运行中，依次执行 `record start → record stop → record start → record stop`，且每次停止均已完成，会生成两份独立的 `record_*` 会话目录；上一级的 `rc_<run_id>.jsonl` 仍为同一份追加日志。已经录制时重复 start、已经停止时重复 stop 都不会创建额外会话。前一段还在 stopping／排空时再次 start 会报告状态转换进行中，需要等停止完成。

## 自动录制与成功后换段

```text
record auto        # 切换自动模式
record auto on     # 开启；没有录制时立即发起录制
record auto off    # 关闭自动模式，当前录制继续
record status
```

auto 默认关闭，仅本次 RC 运行生效，不修改配置文件。输入框上方显示 `recording_auto: on/off`，实际是否正在录制仍由 `recording_state` 表示。重复 `record auto on` 保留已有会话；如果正在停止，则等待旧会话完成，再确保新录制就绪。关闭 auto 不撤销已经明确请求的录制启动。

auto 开启后新发起的 `reset`、`pose restore`、策略切换（含 `--reset`）、策略链 apply、单实例／全组 reload、reinitialize、冷重试，以及配置实际触发的 reset／reinitialize，在**成功完成后**后台结束旧录制并开启新录制。操作过程留在旧会话中；原操作回执不等待换段。参数拒绝、失败、取消、普通配置更新、保存／查询不换段；pause、resume、take、return 的内部上下文重置也不换段。一次顶层操作只触发一次。

`record start`、`record stop` 和裸 `record` 不修改 auto。手动 stop 会使尚未完成的旧操作和排队换段失效，旧回调不能再开启录制；之后新发起的重置类操作成功时可恢复录制。关闭 auto 清除尚未开始的自动换段；已提交旧段停止的换段允许完成必要的新段启动，除非又收到手动 stop 或退出。

自动换段采用最多 256 项的串行有界任务队列，与 connector 的现有录制边界共用转移锁。connector 仍在旧设备断开前结束旧会话、新连接就绪后按原流程恢复录制，auto 不额外切第二次；连接切换期间手动 stop 仍优先。此前未录制时，后续新发起的 connector 切换／冷重试成功也可由 auto 恢复录制。

`record status`、总状态的 `recording` 以及录制命令结果包含 `auto`、`auto_pending`（排队及执行中的任务数）和 `auto_error`。换段错误显示在录制状态及日志中，不改写已经成功的原操作回执，不回滚动作或改变控制权；auto 保持开启，不进行无限重试。新会话 `configs/initial.json` 的 `record_auto` 包含触发 `operation_id`、`reason`、`previous_session_id`，并保存启动时的实际配置快照。connector 已有换段继续使用 `connector_switch` 关联信息。

结构化 MCP `record` 新增 `action="auto_on"|"auto_off"|"auto_toggle"`，仍无需 take，并保留现有控制代次检查。`wait` 对 auto 开启命令等待录制就绪；重置／切换命令本身不等待后台换段。退出禁止新的自动启动，自动线程与 recorder 沿用统一清理期限。

## 文件与对齐规则

| 文件 | 内容 |
| --- | --- |
| `rc_<run_id>.jsonl` | RC 本轮运行的追加日志，位于录制目录的上一级；不开启录制时也持续写入 |
| `samples.jsonl` | 30 Hz 采样提交记录、时间、每路 MKV 相对路径、段内帧序号、像素哈希／帧身份、重复或缺失标记，以及本区间采用的 `control_events` 发布引用 |
| `state.jsonl` | 完整机器人缓存状态及 `state_error`；ACOne 包含原始反馈字段 |
| `qpos.csv`、`epos.csv` | 每个采样时刻的 14 维关节／末端状态；前 3 列是 `sample_id,timestamp_ns,monotonic_ns` |
| `control_qpos.csv`、`control_epos.csv` | 该采样区间各 arm **最后一次成功发布**的目标；另一模式、没有发送或未成功的 arm 留空 |
| `control.jsonl` | 每次动作尝试及逐 arm 成功发布，包含模式、7 维值、动作尝试关联和实际时间 |
| `policy.jsonl` | 观察输入、每实例请求与结果、错误／超时／取消、原生链各环节及整体事件 |
| `events.jsonl` | 录制期间的 RC/policy 日志 |
| `llm_failure.jsonl` | 从策略结构化日志中提取的失败检测请求与结果收取事件 |
| `session.jsonl` | 开始、停止、错误、诊断缺口及停止时未完成请求 |
| `configs/` | 开始录制时和后续记录的配置快照，敏感字段脱敏 |
| `video_raw/samples/<camera>/<segment>.mkv` | 30 FPS、FFV1 level 3 / bgr0 可逆 RGB；完整片段 150 帧、五秒，尾段按实际样本数封口 |
| `video_raw/observations/<camera>/<segment>.mkv` | 未匹配采样帧的实际 policy 输入，每 150 张图像一段，不占用主视频时间线 |
| `video_segments.jsonl` | 已封口片段、起始帧、帧数、尺寸及逐帧像素哈希；整场无图的相机记录 `video.unavailable` |
| `recording.lock` | writer 与 reduce 共用的排他文件锁；文件持续存在，是否持锁才表示活动写入 |
| `video_reduction.json` | reduce 已发布且验证的输出所覆盖的原始素材清单 |

新会话声明 `image_storage: mkv_ffv1_v1`。图像引用使用 `file`、`frame_index`、`sha256` 和 `placeholder`，并保留原时间、传感器序号及 policy 图像池身份。哈希覆盖 `str((shape, dtype))` 与 RGB 原始字节。主视频片段为 `sample_id // 150`，段内位置为 `sample_id % 150`，不缩放、不做有损颜色转换。

正常停止会话的四份 CSV 行数和 `sample_id` 与 `samples.jsonl` 一致。CSV、状态文本仍逐样本 flush/fsync，随后写入并同步 `sample_commit`。**新格式的 `sample_commit` 表示文本已提交、视频已受理，不证明图像已经持久完成。** 视频线程流式写入 `.mkv.part`，封口成功、文件 fsync、原子发布及目录同步之后，才写入并同步 `video_segments.jsonl`。读取工具应同时检查该索引；`robot_control.recording_media.read_rgb` 拒绝未封口引用。

异常退出可能留下额外 CSV 尾行、未封口 `.part`、未登记的 MKV 或索引最后一条不完整记录。文本以 `sample_commit` 有效前缀为准，视频只认有效完成索引。已封口段独立可读；当前尾段允许不完整。正常停止排空全部文本和图像，并封口实际长度的尾段，不补足五秒。缺少 `record.stopped` 或存在错误的会话不能当作完整成功，但静态目录中的已封口素材仍可 reduce。普通运行日志原有定期同步方式不变。

`epos` 是框架已有的 14 维末端状态：左右 arm 各 6 个位姿坐标加 1 个 gripper 值。CSV 不补写上次动作：没有真实发布的新目标时留空，避免把持续保持误认为又发送了一次。左 arm 成功、右 arm 发布失败时，仅左 arm 有成功记录。

成功发布之后，观察回调偶尔会受线程调度影响而晚于当前采样边界入队。这次真实发送会保留原始 `monotonic_ns`，进入下一次实际收取它的采样行，并标记 `late_observer`。对齐控制 CSV 时应使用 `samples.jsonl` 的 `control_events` 发布引用，不应仅按单调时间窗口重新分配行。

相机帧保留原始帧序号、传感器墙钟时间和接收单调时间。轮询相同帧会明确标记 `repeated`；相机超时或解码错误不会伪造新图像。缓存中的旧图像保留失效原因，录制提交将无有效图像的相机标记为 `missing`。采样线程错过截止时间则产生 `missed` 记录。视频中的缺失槽使用上一有效图像占位（`placeholder: hold`），首次有效图像前使用黑帧（`placeholder: black`）；均标记 `missing: true, valid: false`，不能当成有效传感器数据。尺寸未知时只保留缺失槽数量，得到尺寸后由视频线程回填黑帧；早期黑帧的尺寸和哈希从完成索引读取。整场无有效图像的相机不生成伪视频。`repeated` 表示传感器帧身份重复，与占位标记分开。录制中分辨率变化会明确失败。

`state_error` 非空时，状态 CSV 仍可能保留最后一份完整反馈缓存；数值存在不代表该时刻又收到了新反馈。判断反馈新鲜度应同时读取 `state.jsonl` 的错误、序号与原始时间，而不能仅依据 CSV 行的 30 Hz 采样时间。

## 推理、链和失败检测

`policy.observation` 保存与本次实际 image lease 对应的 RGB，并使用同一 `observation_id` 关联 policy 输入。image pool 暂时不能发布新帧时，记录应对应 policy 实际拿到的帧，不能将新的相机缓存冒充为该次输入。policy 请求事件只保存共享内存描述符；离线读取应使用 `image_files` 的 MKV 帧引用，不能依赖运行结束后的 SHM 名称。能匹配已受理采样图像的输入复用对应帧，否则写入独立 `observations` 无损流，按 `observation_id` 和原始时间关联。辅助流不是 30 Hz 传感器时间线；去重缓存最多保留 4096 个像素身份。

每个可执行实例分别产生 `policy.start/result/error/cancel/timeout`。原生链的子请求携带 `chain_id`、`chain_request_id`、`cell`，同时保留实例自己的 `request_id`、来源目录身份、代次及配置版本；`a-0` 与 `a-1` 的输出不会合并。整体链事件还记录 `policy.stage_start/stage_result/stage_error`。迟到或过期响应分别记录为 `policy.late/stale`，记录行为不会使它们重新进入控制。

录制可能在推理中途开始或结束。只有结果、没有本段开始事件的请求带 `start_outside_recording`；停止时仍未结束的请求出现在 `session.jsonl` 的 `unfinished_requests`。这些标记表示录制边界，不等同于 policy 故障。

`llm_failure` 使用 `query_id` 关联请求执行和结果收取。日志包括提交／开始／结束／收取时间、HTTP 耗时、使用量、finish reason、解析后的 `failed`、原始响应和采纳／丢弃原因。请求完成与结果被控制逻辑收取是两个不同的时刻。单条结构化诊断有 8000 字节上限；截断时保留原始大小和截断字段标记。密钥与凭据经过脱敏，图片数据不进入诊断行。

## 状态、容量和退出

`record status` 返回 `state`、`session_id`、`path`、`error`、`samples_written`、`samples_accepted`、`repeated_frames`、`missing_frames`、`missed_samples`、`extra_dropped`、`pending_bytes`、`pending_samples` 和 `peak_pending_bytes`；视频工作开始后还包含 `video_pending_frames`、`video_pending_bytes`、`video_segments_written`。

序列化和磁盘写入由独立 writer 进程执行，每路采样／辅助视频各有专用线程和 FFmpeg 编码进程，每个编码器使用两个线程。原始图像立即流式送入编码器，不在内存积攒五秒。writer 主线程继续处理文本，视频管道阻塞不会阻塞控制提交或后续文本处理。视频完成索引使用独立锁，索引 fsync 不持有受理或统计锁；停止等待尾段封口时，普通日志也可继续落盘。默认采样内存额度为 512 MiB，附加诊断额度为 64 MiB，最多 60 个待处理采样。在途字节估算覆盖保留图像、IPC 和编码开销，因此不等于进程 RSS 或磁盘文件大小。只有文本处理完成且原始像素已送入编码器后才归还额度；转交线程本身不归还额度。每路视频队列最多 128 个槽，拥塞、编码器退出或排空超时会明确报错。附加诊断拥塞时记录丢弃数量；必需采样超过额度会停止该段录制并给出错误，保留已写数据。磁盘写入失败、writer 退出或停止排空超时也会形成明确错误。

退出走 RC 的统一绝对期限清理。被迫终止 writer 属于强制清理，RC 返回非零；不能把只有目录存在、只有部分视频 或仍有 pending 数据当作成功停止。

## 批量生成视频并删除原图

TUI 或 headless 可直接执行：

```text
record reduce record_20261113_120000_12345678
record reduce "record_带空格的名称"
record reduce all
```

名称相对于当前 RC 的 `outputs_directory/logs`，必须是该目录下的文件夹名，不能传绝对路径或 `../`。`all` 递归扫描全部会话，指定名称只扫描对应目录。命令在独立后台进程执行，不暂停策略、不切换录制、不改变控制权；日志输出进度和转换/跳过/失败数量。同一 RC 同时只接受一个 reduce 任务，等待超时不会取消任务；RC 退出会中断转换并收齐子进程，尚未验证完成的原图保留。MCP `command` 执行 reduce 需要 take，原有录制开关无需 take 的规则不变。

TUI 命令与 `scripts/recording_videos.py` 离线入口共用 `robot_control.recording_videos` 的实现。离线脚本只依赖 Python 标准库以及 PATH 中的 `ffmpeg`、`ffprobe`，FFmpeg 需要支持 `libx264`，新录制还需 FFV1 / bgr0；record start 会检查编码能力，缺失时明确失败。RC Dockerfile 已加入 FFmpeg；旧 RC 镜像需重新构建后才能在容器内使用此命令。脚本默认递归扫描项目的 `outputs/logs/`，也可指定日志根目录或单个 `record_*` 会话目录。

先预览处理范围：

```bash
python3 scripts/recording_videos.py --dry-run
```

处理全部静态录制目录，包括异常结束的会话；活动 writer 持锁的会话会跳过：

```bash
python3 scripts/recording_videos.py
```

指定其他位置、调整帧率或重新生成已有视频：

```bash
python3 scripts/recording_videos.py /data/robot-logs --dry-run
python3 scripts/recording_videos.py /data/robot-logs
python3 scripts/recording_videos.py /data/robot-logs --fps 30 --overwrite
python3 scripts/recording_videos.py /data/robot-logs --crf 24 --preset slow
```

每个会话输出 `videos/head.mp4`、`videos/left_wrist.mp4`、`videos/right_wrist.mp4`，以该会话实际存在的相机目录为准。新默认采用 H.264 / yuv420p、**CRF 28 / preset medium**，比原 CRF 18 / fast 更偏向节省体积，允许细节损失；保留分辨率和默认 30 FPS。脚本支持 `--crf 0..51` 和 `--preset` 调整；CRF 越高越偏向小文件。奇数尺寸在右侧／底部补齐到偶数。PNG 按数字采样编号排序，编号不连续也可处理，缺失编号会报告但不补帧。因此旧 PNG 视频是已有图像的连续预览，存在缺帧时不能直接以视频时间对齐原始 CSV。新 MKV 则按完成索引排序，保留占位帧和原有 30 Hz 采样时间线，不允许用 `--fps` 改变它。

每个会话根目录还会保存 `initial.png`。旧 PNG 会话从 `images/head/` 中选择数字采样编号最小的现有 PNG，逐字节独立复制并校验，不从视频截图，也不改变原图尺寸或像素。即使最早几个采样缺少 head 图像，也会选第一张实际存在的 head 图片。新 MKV 从已封口主视频中第一张有效、非占位 head 帧无损解码，再逐像素验证 `initial.png`。缺少有效 head 帧时报告错误并保留原始目录。

脚本先在临时目录复制初始状态图片并转换视频，逐路解码核对视频帧数、尺寸及帧率，全部通过后发布并刷盘初始状态图片和 MP4，最后删除该会话的 `assets/` 和 `images/`。新 MKV 还会完整解码所有待处理主片段和辅助片段，核对逐帧像素哈希；所有输出验证、发布、同步之后，才删除本次覆盖的片段。未封口、未索引素材明确报告并保留；已索引素材损坏或缺失则本次失败，不删除原始文件。初始状态图片、视频、JSONL、CSV 和配置快照保留。正常结束会清理临时文件；复制、转换或校验失败会保留 `assets` 和 `images`，继续处理其他会话，并以非零退出码报告失败。

reduce 与新 writer 共用 `recording.lock` 的 `flock` 排他锁，从扫描到发布、删除期间持续持有。活动 writer 或其他 reducer 持锁时跳过，不依赖 PID、目录存在或 `session.jsonl` 的结束状态。异常退出后系统释放锁，静态目录即可处理，不要求 `record.stopped`。`--dry-run` 不创建锁文件、不修改任何素材；旧版 PNG writer 没有该锁协议，因此升级前应停止旧进程后再处理其目录。

有任一路目标 MP4 或 `initial.png` 已存在且原图仍保留时，默认跳过整个会话；显式使用 `--overwrite` 才会重新生成、验证并删除原图。旧 PNG 原图目录已经删除的会话会直接跳过，重复运行不改动已有结果；若此前未保存初始状态图片，会提示无法补存无损原图。新 MKV 的 `video_reduction.json` 记录完成索引摘要和全部已发布输出的 SHA256；删除途中被取消后，重跑会先核验回执、全部输出及剩余原始片段，再继续清理，输出损坏时保留剩余素材。符号链接会话不会被递归跟随，符号链接输入／输出／assets 目录和初始状态图片目标会被拒绝。

删除原始素材后，只额外保留 head 首帧的无损 PNG，日志中对应的 PNG 或 MKV 图像引用会失效；JSONL 本身保持原样，不会被改写为视频或初始状态图片引用。视频采用有损压缩，且不包含仅存在于旧 `assets` 或新 `observations` 流中的额外策略观察图像，因此此结果用于视频回看，不能替代原始无损图像或完整策略输入数据集。

## 可复现验收和压测

### 本机 PNG / MKV 对照 benchmark

```bash
.venv/bin/python scripts/bench_recording.py --output outputs/recording-mkv-bench
```

脚本从 Git `6003d3a` 提取旧 writer，使用同一当前 Python、同一磁盘、同一 512 MiB 采样／64 MiB 附加额度、三路 640×480 输入和文本负载。450 样本吞吐测试三轮交错执行；随后两种实现分别运行移动加噪声与全噪声的 60 秒、30 Hz 实时负载。每样本附带策略观察，每 31 样本加入一张策略专用图像。吞吐测试在额度范围内背压以测完整处理能力；实时测试不等待额度，明确记录拒绝／丢失。可选观察拒绝不提前结束 60 秒采样，必需采样失败则报告失败时间和原因。

计时包含停止排空与尾段封口，计时后完整解码核对有效采样帧和策略输入哈希，并检查四份 CSV 行序。证据包含环境与源码哈希、逐次入队延迟、进程树 RSS 抽样、CPU、文件数／去重硬链接字节数、Python writer 的 `os.fsync` 调用次数、文本与视频完成情况。FFmpeg 内部系统调用不在 Python fsync 计数内。失败轮不作为速度优势。默认 benchmark 不使用机器人、ROS 或业务模型；本机测量不能代替服务器真机验收。

`tests/manual/recording_acceptance.py` 使用真实 `RecordingService`、SDK host/worker、独立 connector 进程、Unix socket、POSIX 共享内存和 loopback HTTP 模型。默认以三路持续变化的 640×480 RGB、30 Hz 相机／录制、50 Hz 控制提交运行 60 秒。它逐个核对采样提交和 CSV，以逐 arm 实际发布回执重算控制 CSV，并对 PNG 或已封口 MKV 全部解码并做无损像素哈希验证；每次模型实际收到的三路像素还会与保存的观察输入比较。

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

ROS 命令需将 `--docker-image` 换为 `connector/acone/Dockerfile --target test` 构建的测试镜像。它启动独立 ACOne connector 和已有 `robot_control_test_sim` 进程，使用真实 ROS 2 消息、压缩图像订阅和命令发布；connector 到录制端的图像仍通过共享内存复制。仿真反馈为 90 Hz，相机每三个反馈周期发布一次，即 30 Hz。除了录制文件对齐，还逐 arm 核对仿真端实际收到的模式和 7 维目标；回执与 simulator 日志保留在证据目录。ROS namespace、domain 和容器部署标签均独立；不启动厂商驱动。ROS 模式使用固定合成 RGB 图像，动态编码压力由前述 Mock 压测覆盖。

证据包括 `report.json`、`status-history.json`、`model-requests.json`、policy host 日志和完整录制文件；Docker 运行另存 `deployment.json`、`teardown.json`、`container.log` 及源码快照。报告给出实际采样／控制频率、各路缺失和重复、源帧序号跳跃、队列字节峰值、轮询观察到的队列采样峰值、动作提交及提交前记录总耗时的 p50/p95/p99/max，以及控制启动迟到量。提交耗时不代表设备发布或到位耗时，逐臂发布时间保留在回执事件中。`teardown.json` 按本轮唯一部署标签核对容器是否全部退出；发生强制容器移除时验收命令返回非零。

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
