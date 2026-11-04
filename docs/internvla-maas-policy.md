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

RC `frequency_hz` 必须手工设为服务的 `control_hz`，默认均为 30。特别是当前 Acone 模板为 100 Hz，接入此策略前需修改所用私有 RC 配置并重启 RC。客户端不插值，也不自动修改 RC 配置；SDK 没有暴露 RC 频率给策略，因此客户端只能校验服务与 `expected_control_hz`，不能代替这一步部署核对。

RC 外层超时应留出余量。使用上述默认值时，建议 `timeouts.initialize_s >= 35`、`timeouts.inference_s >= 65`、`timeouts.release_s >= 8`；仓库默认初始化/释放期限已足够，但默认推理期限 60 秒应增加。保留现有 RC 安全限速配置；限速未完成目标时不消费动作行。

## 运行与冷切换

```text
switch internvla_maas
resume
pause
policy internvla_maas switch-model model_alt.json
resume
```

所有配置都是冷配置。`switch-model` 先做文件/字段校验，返回通用冷补丁；服务握手在旧实例退出证明齐备后的新实例初始化中执行。目标路径相对本策略目录，或为容器可访问的绝对路径。命令成功后保持暂停。链内使用明确实例名，例如 `policy internvla_maas-0 switch-model model_alt.json`。

每个实例独占容器、执行进程、后台网络线程和 WebSocket。客户端不会为重复实例自动生成或修改 robot_id；配置相同身份的并发实例会被网关以 4409 拒绝，不能把来源共用理解为连接共用。

## 协议和动作边界

使用 `maas-robot/1` 二进制 WebSocket 和 `msgpack-numpy` 数组格式，严格一问一答。按交接文件的 Python 参考实现编码；不支持正文另述的 `__ndarray__` 格式，也不自动回退。解码前拒绝对象、结构化数组和异常 buffer/shape，避免进入 pickle 回退；输入动作消息上限为 1 MiB，不限制为这一大小的输出原始图像。

握手顺序是 `server_hello → robot_hello → ready`。校验模型、协议、相机角色、`joint_pos` 状态和动作字段、50 行 horizon 及执行频率。机器人声明 14 自由度、raw 图像和绝对关节动作空间。

相机映射为 `head → front`、`left_wrist → wrist_left`、`right_wrist → wrist_right`。图像以原始尺寸的 RGB uint8 HWC 发送，服务负责模型所需的缩放；状态顺序保持左臂 6 关节、左夹爪、右臂 6 关节、右夹爪，不换算单位。观测含递增 seq、秒级时间戳、新会话 episode_id 和任务文本。

只接受有限的浮点 `[50,14]`、`space=joint_pos`、`mode=absolute` 动作。客户端不做反归一化或 delta 累加。重叠区手臂采用原 Qwen 指数平滑，夹爪保留旧计划；补偿只加在输出副本上，不累计到 canonical 缓存。仅 RC 控制线程发送和消费动作。

后台请求只使用 SDK 已复制的图像，不保存 POSIX 共享内存视图。普通 reset 取消并收齐旧请求，关闭旧连接、清空轨迹后重新握手，建立新 episode；旧结果不会进入新控制代次。不发送回执未定义的远端 reset 或 episode_result 消息。

断线、超时、401/404、4400/4409、非法消息、服务端 error/stop 都进入故障流程，停止使用动作缓存；不重发观测、不自动重连，不把 stop 当成任务完成。普通 reset 不清除故障。修正原因后手动 `switch internvla_maas`；若处于局部重载失败待恢复状态，按 RC 提示使用 `retry-reload <实例>`，随后显式 resume。服务端尚未释放旧身份时重新握手也可能返回 4409，仍按此规则处理。

## 验证

安装 RC 测试依赖，以及本策略锁定的依赖后执行：

```bash
python3 scripts/test_scope.py --policy internvla_maas
```

这会运行 example 基线和 MaaS 专属测试。协议测试使用本机 WebSocket 替身，worker 测试使用真实 SDK host、子进程和共享内存；不会请求真实模型或启动硬件。

新镜像的 test target 可验证 Python 3.11/Linux 协议与调度。`tests/internvla_maas_docker_smoke.py --evidence-dir <目录>` 使用隔离部署，验证 RC Python 3.10 到策略 Python 3.11、正式 Compose、冷切换、故障停止和手动恢复。其 `RC_TEST_RC_IMAGE` 须包含 RC 测试依赖及测试服务所需的 websockets、msgpack、msgpack-numpy；这些附加依赖不进入正式 RC 镜像。还应运行 `tests/example_docker_smoke.py` 覆盖 example 自链。

真实服务连通性、实际训练单位/机位以及真机动作单独验收；本地替身和容器通过不代表这些项目已通过。
