# LIFT2 connector

`lift2` 是独立 ROS 1 Noetic / CPython 3.10 connector。支持双臂及夹爪关节目标、
关节与末端状态、三路 RGB、预览和录制，使用现有 RC/connector SDK 接口。
RC 与 policy 不加载 ROS。底盘、升降、头部和 VR 不在此适配器的控制范围内。

控制周期由 RC 管理，LIFT2 与 ACOne 统一使用 `frequency_hz: 30`。切换 connector
不会改变频率；已有 RC 配置修改后需重启 RC 生效。

## 配置与启动

```bash
./setup.sh --policies example --connectors mock,lift2
```

setup 只创建缺失的设备配置及命名 profile，不覆盖私有配置。编辑
`connector/lift2/devices/robot-main.yaml`，必须填写：

```yaml
ros_master_uri: http://机器人ROS主机地址:11311
ros_ip: 运行connector的Linux主机IP
```

这里的 `ros_ip` 必须是机器人能反向连接的本机 IP，而不是容器桥接地址。
ROS 1 TCPROS 除 master 端口外还使用节点协商端口，需要双向网络可达。
connector 使用 host network；其进程会清除继承的 `ROS_HOSTNAME`，避免覆盖设备配置。
模板中的两个值故意留空，不会沿用参考推理脚本中的旧实验室地址。

设备驱动和相机由现场独立启动。连接器不会启动 roscore、CAN、厂商驱动或模型。
启动当前部署后，在 RC 输入：

```text
connector switch lift2
```

也可把当前 RC 配置中的机器人选择改为 `robot: {profile: lift2}`，在下次启动时连接。
使用默认基础 Compose 时运行 `./scripts/compose.sh up -d --no-build`。
连接成功、切换成功后 policy 保持暂停。

单独重建镜像：

```bash
./scripts/build.sh connector:lift2
```

## 接口映射

| 用途 | 默认 topic | ROS 1 类型 |
| --- | --- | --- |
| 左臂关节反馈 | `/joint_information` | `arm_control/JointInformation` |
| 右臂关节反馈 | `/joint_information2` | `arm_control/JointInformation` |
| 左臂末端反馈 | `/follow1_pos_back` | `arm_control/PosCmd` |
| 右臂末端反馈 | `/follow2_pos_back` | `arm_control/PosCmd` |
| 左臂关节目标 | `/joint_control` | `arm_control/JointControl` |
| 右臂关节目标 | `/joint_control2` | `arm_control/JointControl` |
| head RGB | `/camera_h/color/image_raw` | `sensor_msgs/Image` |
| left_wrist RGB | `/camera_l/color/image_raw` | `sensor_msgs/Image` |
| right_wrist RGB | `/camera_r/color/image_raw` | `sensor_msgs/Image` |

topic 可在设备 YAML 中覆盖。`qpos/qvel/effort/eef` 均按左臂 7 维、右臂 7 维排列。
每臂前 6 项为关节或末端位姿，最后一项为夹爪。关节和夹爪数值按设备原生单位传递，
不做推理脚本中的夹爪阈值化，也不做 delta→absolute 转换。

`eef` 的夹爪项使用对应关节反馈的第 7 项。部分厂商配置的 `PosCmd.gripper`
与关节反馈单位不同，原值保存在 `raw.left.eef`、`raw.right.eef`，关节原始消息保存在
`raw.left.joint`、`raw.right.joint`。末端位姿目前仅供查询。

双臂关节和末端反馈均有效、两个命令 topic 的订阅连接完成后才就绪，默认反馈超时 2 秒。
状态保留关节与末端各自时间；没有 Header 的消息使用实际接收时间。
反馈序号随关节采样递增，每臂保留最近 512 条历史，历史项捕获当时可用的末端反馈。
查询和后续末端更新不会刷新或改写已保存的历史采样。

所有现有查询继续使用 `describe/get_state/get_images/release_images`，不增加 RPC 版本。
旧数据仍可供录制/预览诊断，但缺失、过期或异常数据不会标成有效。

## 图像

默认 `camera_transport: raw`，支持 `rgb8`、`bgr8`、`rgba8`、`bgra8` 和 `mono8`，
正确跳过每行填充字节，统一转换为连续、只读 RGB `uint8`。
不支持的编码及异常尺寸/长度明确报错。帧上限为 1600 万像素、64 MiB 消息数据。

改用压缩图像时设置 `camera_transport: compressed`，并将三个 topic 改为实际
`sensor_msgs/CompressedImage` topic（通常添加 `/compressed`）。压缩解码使用 Pillow，
不使用 `cv_bridge`。每相机只保留一个待解码样本，解码不占用动作线程。

共享内存仍由现有 connector SDK 管理：没有 `get_images` 请求就不发布快照；
socket 只返回描述符，RC 只读映射、复制后明确释放输入租约。policy 继续使用 RC 图像池。

## 动作与关闭

仅接受 `qpos` 绝对目标，每臂 7 个可由 ROS `float32` 表示的有限数值。发布前校验完整请求及目标臂订阅连接，
使用独立消息对象，同步调用 ROS 发布接口，复用 SDK 的单一 FIFO。
每次成功发布记录对应臂事件；成功回执表示发送函数返回，不表示硬件到位。

**任何包含 `epos` 的请求都会整条失败，左右臂均不发布。** 包括单臂、双臂和混合
`qpos/epos` 请求。错误通过现有失败回执返回，RC 会进入既有发布故障路径，
不消费该目标并停止新增动作；它不会静默跳过错误后继续推理。
请使用产生 `qpos` 的 policy；适配器不改变 policy 输出或增加模式回退。

已提交动作不因 take/pause/switch 自动取消或重发。关闭时由 SDK 排空队列，适配器
在期限内注销 ROS 资源并停止解码线程。初始化或清理阻塞会明确失败，外层生命周期
管理器按既有期限处理进程退出证明，不把超时当作干净退出。

## 镜像与来源

运行镜像是固定摘要的 CPython 3.10，依赖版本位于 `connector/lift2/requirements.lock`。
Noetic 构建阶段仅生成 Python 消息并提取所需的纯 Python ROS 包；运行层没有
Conda、CUDA、模型框架、`cv_bridge` 或厂商硬件驱动。
ROS Python 路径通过 site `.pth` 加载，兼容 RC 为 SDK 设置的 `PYTHONPATH`。

三份厂商消息保持原字节，来源版本、SHA256 和 ROS MD5 见
[消息来源记录](../ros_ws/lift2/arm_control/SOURCE.md)。独立的 catkin 包位于
`ros_ws/lift2`，不进入 ACOne 的 ROS 2 消息构建。

## 定向验收

单元与已有 connector 定向回归可使用仓库测试环境运行：

```bash
.venv/bin/python -m pytest -q tests/test_connector_lift2.py tests/test_lift2_setup.py \
  tests/test_connector_drivers.py tests/test_connector_images_setup.py \
  tests/test_connector_config.py tests/test_connector_control.py \
  tests/test_connector_proxy.py tests/test_connector_transport.py
```

已有 RC、anchor、example、mock、lift2 镜像后，额外构建专用模拟器并运行 LIFT2 专项：

```bash
docker build --target simulator -t robot-control-compose-lift2-simulator:test \
  -f connector/lift2/Dockerfile .
python3 tests/lift2_docker_acceptance.py --evidence-dir /tmp/lift2-evidence
```

宿主 Python 需具备 setup 使用的 `ruamel.yaml==0.19.1`。验收使用独立临时部署，
模拟器 `--network none`；仅测试配置将 lift2 connector 加入该网络命名空间。
覆盖真实 ROS 1 消息传输、原始/压缩 RGB、FIFO、拒绝 epos、共享内存租约、反馈过期、
example/自链、录制、连接切换时 policy 容器与进程保持，以及清理证明。
脚本只清理自身容器和卷，证据保留在指定目录。

这是定向模拟验收，不是完整接口回归或整套 Docker smoke，也不代表真机验收。
当前实际执行结果见 [验证记录](verification.md)。
