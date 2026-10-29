# Ubuntu 22.04 amd64 部署

目标环境为本地 Linux Docker daemon、ROS 2 Humble / Python 3.10 RC；宿主无需 Conda或项目 Python 包。厂商硬件驱动由操作者独立启动和验收，本项目不会启动它。

1. 按 [Docker 官方 Ubuntu 安装文档](https://docs.docker.com/engine/install/ubuntu/) 安装 Docker Engine 与 Compose 插件。让日常用户能够访问 Unix socket，重新登录后验证 docker info；不要把 socket chmod 为 666。
2. 将本独立仓库放到稳定路径，用普通用户运行 `./setup.sh --config-only`。确认 configs、policy、outputs、snapshot 可由此用户读写。
3. 编辑 `configs/rc.mock.yaml`，初次建议 startup_policy 为 null；真实配置在 `configs/rc.acone.yaml`。不填旧 host/socket 表。
4. `./setup.sh --policies hold` 构建基础镜像和 Python 3.14 策略镜像。其他策略按目录选择；Qwen 需先准备 GPU、NVIDIA Container Toolkit、模型和缓存目录，参见 Qwen 文档。
5. `./scripts/compose.sh up -d --no-build` 启动两个基础容器，再 `./scripts/compose.sh attach robot-control`。先检查 status 和 policies，再 switch hold；初始化后暂停，只有 resume 才推理。
6. 使用 exit 退出 RC，再 `./scripts/compose.sh down` 完成动态容器与外部资源收尾。

## ROS 仿真与真机

```bash
# 仿真：第三个容器是显式启用的 ROS 测试模拟器
./scripts/compose.sh -f compose.ros-sim.yaml up -d --no-build
./scripts/compose.sh attach robot-control
# 真机：先核对 ROS 域、网络、话题和 safety；硬件驱动另行启动
./scripts/compose.sh -f compose.acone.yaml up -d --no-build
```

切换运行方式前先 down。厂商消息仅从 ros_ws/src 构建，vendor 备份不参与构建。MCP 默认只读，默认端口仅绑定宿主 loopback；显式接管/归还遵守控制权代次检查。

## 退出与故障

stop_grace_period 必须长于 RC 的 shutdown_s。`BLOCKED_CLEANUP` 表示本地或外部读者未取得证明，不能靠删除 socket、卷、恢复账本或重建 anchor 绕过。恢复 Docker/远端服务后重试 down。

anchor 被重建时存活 RC 仍在旧 IPC namespace；必须一起重建 RC 与 anchor。setup 会记录 socket 实际 GID，不假定 999；anchor 常驻进程已降权，只有卷初始化使用 root。

## 终端图像

初次 detached 启动可能只有 0×0 PTY，图像初始化使用缓存的默认 10×20 cell size；连接真实终端后应验证 Kitty/Sixel 显示效果。终端画面、相机帧、ROS 仿真与真机验收分别记录。

测试命令和证据范围参见 [verification.md](verification.md)。模型输出和真实机器人动作必须由现场验收确认。
