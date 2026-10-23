# robot-control-compose

独立的 Docker 机器人控制框架。主容器为 Ubuntu 22.04 / ROS 2 Humble / Python 3.10，policy 为 Python 3.14。仅部署 hold；多个 policy 可在同一容器同一执行进程中运行，也可映射到不同容器。

图像通过 posix-ipc + mmap 只读共享内存传输，控制消息走 UDS 上的长度前缀 JSON RPC。全部依赖在镜像内安装。

## 启动

需要 Docker Engine 与 Docker Compose 2.24.4 或更新版本。在项目根目录运行：

```bash
docker compose build
docker compose up -d
docker compose attach robot-control
```

启动后保持暂停。输入 `resume` 开始保持当前位置，`pause` 暂停，`exit` 清理并退出 RC。随后 `docker compose down` 停止监督服务和 IPC 容器。不要使用 exec 启动第二个控制器。

```bash
# headless：先停止已有控制器，再启动唯一的交互实例
docker compose stop robot-control
docker compose run --rm --service-ports robot-control python3 -m robot_control --headless --config /workspace/configs/rc.mock.json --mcp 0.0.0.0:4000

# 独立 ROS 模拟器（不会连接真实硬件）
docker compose --profile ros-sim -f compose.yaml -f compose.ros-sim.yaml up -d
docker compose attach robot-control

# Linux 真机：先按实际 ROS 网络/域和话题修改配置
docker compose -f compose.yaml -f compose.acone.yaml up -d
```

真实 ACOne 消息包在主镜像内从 `ros_ws/src/arx5_arm_msg` 编译。厂商完整备份位于 `vendor/arx_x5_ws`，不参与默认构建、不启动硬件驱动。配置目录、policy manifest、outputs 和 snapshot 持久化在宿主项目中；MCP 默认仅通过宿主 127.0.0.1:4000 访问。

## Policy 部署接口

配置 schema_version 为 2。manifest 的 `policy_host` 对应 RC 配置 `policy_hosts` 中的名称，例如 `"hold": {"socket": "/run/robot-control/hold.sock"}`。同一 host 下所有已加载 policy 共用一个执行进程，但模块包、context 和配置独立。辅助模块必须使用相对导入，资源路径使用 `__file__`，不要修改进程工作目录。

新增策略保留 initialize / policy / reset_context / release 四个同步入口；主框架只检查文件存在，语法由对应 host 的 Python 3.14 预检。热更新遵守 hot_reloadable 叶路径白名单，冷更新重建运行组。链 A/B/A 复用 A 的上下文，同一轮全链使用同一份图像。

policy 回调中的图像是借用的只读视图，不得保留到调用结束之后；需要长期保留时显式 copy。任何硬超时、崩溃或视图逃逸会终止该 host 的整个执行进程，等待手动 switch / 链应用后再 resume。断连不代表读者已退出；无法确认清理时需整体 down，重建 IPC 域后重新启动。

主框架参数全部冷改，配置保存仍通过 save-config，快照仍通过 snapshot。初始化/推理/release 默认期限为 600/60/30 秒；调整 release 期限时同步增加 Compose 的 stop_grace_period。

## 开发与验收

详见 [验收记录与重现命令](docs/verification.md)、[Policy 编写与多容器部署](docs/policy-authoring.md) 和 [迁移计划](docs/superpowers/plans/2026-10-23-docker-migration.md)。mock、真实 ROS 模拟器与真实机器人验收分别记录。旧 Conda launcher 专属检查保留为迁移记录，实际生命周期由新 host 集成测试验证。

```bash
docker compose -p rc-compose-validation -f compose.yaml -f compose.test.yaml build integration
docker compose -p rc-compose-validation -f compose.yaml -f compose.test.yaml run --rm integration
docker compose -p rc-compose-validation -f compose.yaml -f compose.test.yaml down -v
```

厂商资源来源及消息副本关系见 [vendor/README.md](vendor/README.md)。真实机器人未在本轮验收中连接；Linux host-network 配置保留供硬件部署使用。
