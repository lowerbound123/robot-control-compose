# InternVLA MaaS 网络策略

`internvla_maas` 把三路相机和关节状态发给已部署的 MaaS 服务，取得 50×14 的绝对关节目标。异步补块、已消费前缀裁剪、指数平滑和夹爪计划沿用 `qwen35_single` 的 SDK 调度；镜像为独立 Python 3.11 CPU 环境，不需要 GPU、模型权重或 Qwen vendor。

## 准备

```bash
./setup.sh --config-only
# 编辑 policy/internvla_maas/model.json
./setup.sh --policies internvla_maas
```

setup 从示例生成私有 JSON 和 schema 3 的 `policy.yaml`，以 0600 写入；私有配置已被 Git 忽略。服务必须事先部署并为当前机器人注册身份。模板不包含可用凭据，空的必填字段会在策略初始化时被拒绝。

`params.model_config_path` 默认引用本策略目录中的 `model.json`。地址、凭据和任务文本都由策略进程读取，manifest 只保留配置路径。

| 字段 | 含义 |
| --- | --- |
| `service.address` | `ws://` 或 `wss://` 基地址，可含网关路径前缀，不含用户名、密码、query 或 fragment。客户端追加 `/v1/robot/<model>/act?robot_id=<robot_id>`。 |
| `service.model` | 默认 `internvla_a15`，与 `server_hello.model` 一致。 |
| `service.robot_id` / `api_key` / `embodiment` | 服务端已注册且匹配当前机器人的值。一个 robot_id 同时只允许一个连接。 |
| `service.connect_timeout_s` | 默认 30 秒，覆盖连接和完整应用握手的总期限。 |
| `service.close_timeout_s` | 默认 2 秒，关闭 WebSocket 的期限。 |
| `runtime.instruction` | 必填任务文本，随每个观测请求发送。 |
| `observations.state_offsets` | 默认 `{}`，按字符串下标 `"0"` 至 `"13"` 给发送的 state 加偏移，不修改 RC 的原始 state。 |
| `execution.chunk_size` / `request_when_remaining` | 固定 50 / 30；少于 30 个剩余动作才预取。 |
| `execution.inference_timeout_s` | 默认 60 秒，覆盖请求发送和响应接收；SDK 同时按提交时间检查结果期限。 |
| `execution.expected_control_hz` | 默认 30，握手时必须与服务声明一致。 |
| `actions.gripper_compensation` | 默认 `true`：第 6、13 维大于 -2.0 时加 0.15。服务端已提供该补偿时设为 `false`。 |

连接位于策略容器中：地址中的 `localhost` 是该容器自身。访问宿主服务可使用 Compose 已配置的 `host.docker.internal`；访问远端则填写容器可达地址。客户端关闭环境变量代理自动发现，`wss://` 使用默认可信 CA 验证证书。不会创建 SSH 隧道。

RC `frequency_hz` 必须与服务的 `control_hz` 一致，ACOne、LIFT2 与当前 RC 模板统一为 30 Hz。已有部署修改所用私有 RC 配置后需重启 RC。客户端不插值，也不自动修改 RC 配置；SDK 没有暴露 RC 频率给策略，因此客户端只能校验服务与 `expected_control_hz`，不能代替这一步部署核对。

RC 外层超时应留出余量。使用上述默认值时，建议 `timeouts.initialize_s >= 35`、`timeouts.inference_s >= 65`、`timeouts.release_s >= 8`；仓库默认初始化/释放期限已足够，但默认推理期限 60 秒应增加。保留现有 RC 安全限速配置；限速未完成目标时不消费动作行。

## 执行配置与服务切换

新模板默认 `execution.mode: async`；`sync` 同样支持。`call_every_actions` 使用通用配置规则，允许正整数，不再强制等于 1。`async` 在策略回调在途时继续消费旧缓存；模型请求本身仍严格串行，耗尽缓存后保持当前目标等待结果。

模板将 `execution.mode` 和 `execution.call_every_actions` 放入热修改白名单，可使用：

```text
policy internvla_maas config set execution.mode "async"
policy internvla_maas config set execution.call_every_actions 3
monitor internvla_maas
```

上述执行字段由策略 configure 解析，普通修改经 RC/SDK 两端确认后生效，保留当前运行/暂停状态、模型连接和统计。新配置入口不要求将字段加入 `hot_reloadable`；已有自定义命令的 update_config effect 仍遵循其白名单。已有私有 `policy.yaml` 不会被 setup 覆盖。MaaS 动作为关节目标，`control_mode` 仍须为 `qpos`。

```text
policy switch internvla_maas
resume
pause
policy internvla_maas switch-model model_alt.json
resume
```

`model.json` 中的服务、推理和动作配置仍为冷配置，`params.model_config_path` 不能热改。`switch-model` 先做文件/字段校验，返回通用冷补丁；服务握手在旧实例退出证明齐备后的新实例初始化中执行。目标路径相对本策略目录，或为容器可访问的绝对路径。命令成功后保持暂停。链内使用明确实例名，例如 `policy internvla_maas-0 switch-model model_alt.json`。

每个实例独占容器、执行进程、后台网络线程和 WebSocket。客户端不会为重复实例自动生成或修改 robot_id；配置相同身份的并发实例会被网关以 4409 拒绝，不能把来源共用理解为连接共用。

## 请求监控

本地 TUI 使用 `monitor internvla_maas` 切换面板；重复实例使用明确实例名，例如 `monitor internvla_maas-0`。

| 字段 | 含义 |
| --- | --- |
| `avg_latency_ms` | 当前 context 内已结束观测请求的平均延迟，毫秒。 |
| `median_latency_ms` | 同一批请求的精确中位延迟，毫秒。 |
| `last_latency_ms` | 上一次已结束观测请求的延迟，毫秒；请求在途时保留上一次值。 |
| `failed_requests` | 没有得到有效动作的请求次数，单次请求最多计一次。 |

延迟从发起 `obs` 发送尝试计时，至动作接收、解码及校验完成，或请求失败为止；不包含握手、发送前准备或断开清理。服务端 error/stop、非法动作、超时和在途断线计为失败，其耗时也计入平均值和中位数。初始化握手、发送前校验失败以及生命周期主动取消不计入统计。

无已完成请求时三个延迟字段显示 `—`，失败次数为 0。普通暂停/恢复、reset 重连和执行参数热修改保留累计值；冷重载或同进程重新初始化创建新 context 时清零。monitor 仅读取有锁的标量快照，不发请求、不等待推理，也不改变故障处理方式。中位数在请求完成时增量维护，监控读取不遍历请求历史。

### 策略内分阶段计时

除上述四项外，每个阶段提供 `avg_<阶段>_ms`（累计平均）、`last_<阶段>_ms`（最近一次）和 `<阶段>_samples`（样本数）。无样本显示 `—`，不是 0；阶段计时仅保存计数、平均值和最近值，存储量不随运行时长增长。

| 阶段 | 实际计时边界 |
| --- | --- |
| `snapshot_submit` | SDK 创建自有观测快照的整个调用，包含状态/图像校验、三路图像复制和提交后台任务；不是单独的内存复制时间。 |
| `dispatch` | 后端向网络事件循环提交协程，到该协程开始运行的派发等待。 |
| `prepare` | 网络请求中的状态转换、偏移、图像校验、缩放和消息字段准备；包含 `resize`。 |
| `resize` | 三路图像合计的等比缩放和居中补黑边耗时，输出固定为 224×224。 |
| `pack` | 完整观测的 MessagePack 编码，包含 ndarray 转字节。 |
| `send` | `WebSocket.send()` 调用耗时，含客户端写入/背压等待，不代表服务端已经收完数据。 |
| `response_wait` | 发送调用结束后，等待完整响应帧返回的时间；包含网络与服务端处理，不能当作纯网络延迟。 |
| `decode` | 响应帧 MessagePack 解码及 ndarray 重建。 |
| `validate` | 动作空间、形状、有限值校验及 float32 输出复制。 |
| `server` | 服务端响应自报的 `server_ms`，不由客户端推算。缺失、负数或非有限值不计样本，最近值显示 `—`，不影响合法动作。 |
| `predict` | SDK 后台推理任务总耗时，包括后端调用、SDK 结果校验和复制；失败时也包含异常收尾。 |
| `result_ready_wait` | 后台任务已完成，到策略开始收集其结果的等待；先开始收集再等待完成时为 0，不含结果交给 RC 后的等待。 |
| `collect` | 收集结果的整个调用，包含尚未完成时的阻塞等待、已消费前缀裁剪、轨迹平滑和夹爪处理。 |
| `postprocess` | MaaS 输出副本及夹爪补偿；属于 `collect` 的一部分。 |
| `callback` | 每次策略 `step()` 回调总耗时，包括仅返回缓存的回调。 |

还提供 `last_source_image_bytes`（缩放前的三路 RGB 图像字节总数）、`last_image_bytes`（缩放后实际发送的图像字节总数，固定 451,584 字节）、`last_request_bytes`（最近编码的完整观测消息字节数）、`last_response_bytes`（最近收到的二进制响应帧字节数）。这些是应用层大小，不含 WebSocket/TCP/TLS 开销。

分段统计记录实际到达的阶段，失败或主动取消时也保留该阶段已经花费的时间；未到达的阶段不增加样本。它们与原有“排除主动取消”的请求总延迟口径不同，且 `predict`、`collect`、`callback` 等存在包含或并行关系，不能把所有平均值直接相加。每项最近值属于最近一次执行该阶段的调用，也可能不是同一请求；可结合样本数判断。

这次只覆盖 MaaS 策略内部，不包含 RC 的相机 JPEG 解码、共享内存发布、结果返回 RC 后的调度或硬件动作下发。

## 协议和动作边界

使用 `maas-robot/1` 二进制 WebSocket，严格一问一答。数组编码以已验证能收到动作的 `diagnose_maas_ndarray.py` 为准：`{b"__ndarray__": True, b"data": array.tobytes(), b"dtype": array.dtype.str, b"shape": array.shape}`。NumPy 数值及布尔标量使用 `{b"__npgeneric__": True, b"data": value.item(), b"dtype": value.dtype.str}`；普通元数据保持 MessagePack 原生类型。

数组元数据固定为字节键。接收端在校验 dtype、shape 和字节长度后，用 `frombuffer(...).reshape(...).copy()` 还原独立持有数据的 ndarray。只支持普通数值和布尔类型，拒绝对象、结构化、复数及畸形数据，不使用 pickle。输入消息上限为 4 MiB，WebSocket 与解码器一致；输出原始图像不受此入站上限限制。

不接受旧的 `msgpack-numpy`（`nd/type/kind/shape/data`）编码，也不自动回退。运行依赖为 NumPy、msgpack、websockets、Pillow 及 SDK 所需依赖，已移除 `msgpack-numpy`。224×224 缩放新增了锁定的 Pillow 12.3.0；更新后运行 `./setup.sh --policies internvla_maas` 重建本策略镜像，再冷重载使用它的实例。

握手顺序是 `server_hello → robot_hello → ready`。校验模型、协议、相机角色、`joint_pos` 状态和动作字段、50 行 horizon 及执行频率。机器人声明 14 自由度、raw 图像和绝对关节动作空间。

相机映射为 `head → front`、`left_wrist → wrist_left`、`right_wrist → wrist_right`。发送前按比例将长边缩放至 224，短边四舍五入且至少保留 1 像素，使用双线性插值，再居中补黑边到 224×224；奇数余量多出的 1 像素留在下侧或右侧。已经是 224×224 的图像保留全部像素。不会拉伸、裁剪、交换 RGB 通道或修改输入图像；每路仍发送 RGB `uint8[224,224,3]` 的 ndarray，不做 JPEG 编码。

每张像素数据为 150,528 字节，三张合计 451,584 字节（441 KiB，约 0.43 MiB），完整请求另含状态、任务文本和少量 MessagePack 元数据。RC 与 SDK 的原始图像和共享内存契约保持不变，缩放仅发生在 MaaS 网络后端。状态顺序保持左臂 6 关节、左夹爪、右臂 6 关节、右夹爪，不换算单位。观测含递增 seq、秒级时间戳、新会话 episode_id 和任务文本。

只接受有限的浮点 `[50,14]`、`space=joint_pos`、`mode=absolute` 动作。客户端不做反归一化或 delta 累加。重叠区手臂采用原 Qwen 指数平滑，夹爪保留旧计划；补偿只加在输出副本上，不累计到 canonical 缓存。仅 RC 控制线程发送和消费动作。

后台请求只使用 SDK 已复制的图像，不保存 POSIX 共享内存视图。普通 reset 取消并收齐旧请求，关闭旧连接、清空轨迹后重新握手，建立新 episode；旧结果不会进入新控制代次。不发送回执未定义的远端 reset 或 episode_result 消息。

断线、超时、401/404、4400/4409、非法消息、服务端 error/stop 都进入故障流程，停止使用动作缓存；不重发观测、不自动重连，不把 stop 当成任务完成。普通 reset 不清除故障。修正原因后手动 `policy switch internvla_maas`；若处于局部重载失败待恢复状态，按 RC 提示使用 `policy <实例> retry`，随后显式 resume。服务端尚未释放旧身份时重新握手也可能返回 4409，仍按此规则处理。

## 验证

安装 RC 测试依赖，以及本策略锁定的依赖后执行：

```bash
python3 scripts/test_scope.py --policy internvla_maas
```

这会运行 example 基线和 MaaS 专属测试。协议测试使用本机 WebSocket 替身，worker 测试使用真实 SDK host、子进程和共享内存；不会请求真实模型或启动硬件。

新镜像的 test target 可验证 Python 3.11/Linux 协议与调度。测试参考编解码器冻结成功诊断脚本的四个函数，独立于生产实现，不依赖下载目录。`tests/internvla_maas_docker_smoke.py --evidence-dir <目录>` 使用隔离部署，验证 RC Python 3.10 到策略 Python 3.11、正式 Compose、冷切换、故障停止和手动恢复。其 `RC_TEST_RC_IMAGE` 须包含 RC 测试依赖及测试服务所需的 websockets、msgpack；这些附加依赖不进入正式 RC 镜像。还应运行 `tests/example_docker_smoke.py` 覆盖 example 自链。

真实服务连通性、实际训练单位/机位以及真机动作单独验收；本地替身和容器通过不代表这些项目已通过。
