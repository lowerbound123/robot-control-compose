# Docker 迁移验收记录

验收日期：2026-10-23。环境：macOS ARM64 宿主、OrbStack Linux ARM64 Docker。实际业务镜像均为 Linux ARM64；本轮没有运行 x86_64 构建或真实机器人。

## 镜像与依赖

| 检查 | 实际结果 |
| --- | --- |
| RC OS/ROS | Ubuntu 22.04 / ROS 2 Humble |
| RC Python / NumPy / posix-ipc | 3.10.12 / 1.26.4 / 1.3.2 |
| Policy Python / NumPy / posix-ipc | 3.14.9 / 2.5.4 / 1.3.2 |
| Policy GIL | `Py_GIL_DISABLED=0`，标准 GIL 构建 |
| 厂商消息 | `arx5_arm_msg.msg.RobotCmd`、`RobotStatus` 导入成功，joint_pos 长度分别为 6、7 |
| 主镜像 / policy 镜像 pip check | 两端均 `No broken requirements found.` |
| 真实 tmpfs 容量不足 | 独立 `--shm-size=1m` 容器分配 2 MiB，明确 ENOSPC，分配计数归零 |

基础镜像由 Dockerfile 中 digest 固定，Python 运行依赖分别锁在 `requirements/rc.lock`、`policy.lock`，测试依赖为 `test.lock`。apt 包使用对应发行版仓库；本次没有建立 apt snapshot 镜像源。主镜像内 colcon 编译厂商消息副本及两个 ROS 测试包；厂商备份不进入构建上下文。

## 已执行的验收

- 最终 Linux / Humble 完整回归：432 passed，30 skipped，13 条第三方弃用提示，163.57 秒；无失败或错误。
- 本机完整回归：428 passed，32 skipped；随后 MCP 退出回执修复的 CLI/terminal 增量回归：33 passed。
- Python 3.10 主容器与两个 Python 3.14 policy 容器的真实集成：6 passed。
- 生产 mock Compose：TUI attach/resume/pause/snapshot、headless、原子配置保存、三路快照持久化、Compose SIGTERM 停止及清理通过。
- 生产 ROS simulator Compose profile：独立模拟器容器通过 DDS 连接主容器，以上 TUI/headless 与停止场景全部通过。
- 独立代码审阅完成；已修复满队列跳过清理、observe 拒绝遗漏租约、host 信号强制清理退出码三项问题，并有回归用例。
- 原 `robot-control` 的 264 个基线文件 SHA-256 校验未变化；两份厂商 `.msg` 与备份逐字节一致。

实际跨容器集成覆盖三相机逐像素只读读取，RPC 仅含描述符且帧消息不足 2 KiB，同 host A/B/A 相同 PID 与上下文复用，不同 host 不同运行实例，同名 helper 隔离，以及双容器慢读者、三槽耗尽、尺寸变化、最终零分配。A 的入口含 Python 3.14 t-string，证明主 Python 3.10 不解析策略语法。

生命周期回归覆盖初始化取消、推理/release 硬超时、os._exit、逃逸图像视图、发送后断连、未确认图像释放、旧结果、冷重载先退出再初始化、MCP 接管边界、暂停、快照与配置持久化。Linux subreaper 用例在初始化和推理中立即派生脱离进程组的子进程后崩溃，检查后代已消失且退出证明成立。背压用例包含 SIGSTOP 后的大报文与满队列。

## 重现命令

在项目根目录运行：

```bash
# 生产和测试镜像
docker compose build
docker compose -p rc-compose-validation -f compose.yaml -f compose.test.yaml build integration

# 实际跨 Python / 跨容器 + ROS 闭环
docker compose -p rc-compose-validation -f compose.yaml -f compose.test.yaml run --rm integration

# 主框架完整 Linux 回归（该组的本地测试 host 使用镜像的 Python 3.10；
# 真正的 Python 3.14 语法与 ABI 边界由上一个集成命令验证）
docker compose -p rc-compose-validation -f compose.yaml -f compose.test.yaml run --rm \
  -e RC_DOCKER_TEST=0 integration python3 -m pytest -p pytest_asyncio.plugin -q -rs

# 独立临时项目、临时挂载；脚本退出时自动 down -v
python3 tests/compose_smoke.py
python3 tests/compose_smoke.py --ros-sim

# 不在业务 IPC anchor 内做内存耗尽试验
docker run --rm --shm-size=1m \
  -v "$PWD/tests/shm_capacity_probe.py:/tmp/probe.py:ro" \
  robot-control-compose-rc:local python3 /tmp/probe.py

docker run --rm robot-control-compose-rc:local python3 -m pip check
docker run --rm --entrypoint python robot-control-compose-policy:local -m pip check

# 仅清理这个验收项目及其 UDS 卷
docker compose -p rc-compose-validation -f compose.yaml -f compose.test.yaml down -v
```

`compose_smoke.py` 不启动第二个业务控制器：每次创建唯一临时项目，使用临时配置/policy/outputs/snapshot，先对现有 TUI attach，再停止 TUI，最后运行 headless。没有操作宿主真实机器人配置，也不遗留容器或卷。

## 跳过项与边界

旧 Conda launcher 专用测试保留但显式跳过；原外部 model-server/bridge 模型不在本轮 hold 交付范围，相关集成需外部 SDK/服务后才启用。旧 Conda ROS fixture 由新的真实 Docker ROS 闭环覆盖。本机缺少 rclpy 和 Linux subreaper 的用例分别在镜像中验证；Docker 专用用例在普通全量组跳过，单独运行。

13 条已知 warning 来自 `textual-image` 的 Pillow `Image.getdata` 弃用提示，不影响当前测试结果。

真机 ACOne 验收尚未执行：真实驱动启动、关节/夹爪方向与单位、限幅参数、网络发现、失联和急停应在硬件环境单独确认。本次 ROS 模拟器通过不替代真机验收。
