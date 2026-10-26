# Ubuntu 22.04 x86_64 从零构建与运行

适用场景：将整个 `robot-control-compose` 项目复制到一台 x86_64 Ubuntu 22.04 电脑，在这台电脑上联网构建镜像。宿主机不需要安装 Conda、ROS、项目 Python 包或 NVIDIA 驱动；当前交付的 hold 使用 CPU。

本文先验证 mock，再验证 ROS 模拟器。此前完整验收在 Linux ARM64 上完成；amd64 是否验收通过，以你在目标电脑执行本文的构建和测试结果为准。

## 1. 确认系统和网络

在目标 Ubuntu 电脑运行：

```bash
uname -m
dpkg --print-architecture
cat /etc/os-release
```

应分别看到 `x86_64`、`amd64`，以及 Ubuntu `22.04` / `jammy`。

首次构建需要访问 Docker 镜像仓库、Ubuntu/ROS apt 源和 Python 包源。构建过程会下载依赖；复制源码目录本身不包含这些依赖。先检查磁盘和内存，构建空间主要占用 Docker 的数据目录：

```bash
df -h /var/lib /home
free -h
```

## 2. 安装 Docker Engine 和 Compose 插件

以下针对尚未安装 Docker 的 Ubuntu 22.04。若已有可用的 Docker Engine 与 Compose，可直接检查版本后进入第 3 步；若安装了发行版 `docker.io`、旧 `docker-compose` 或其他容器运行时，先按 [Docker 官方 Ubuntu 安装说明](https://docs.docker.com/engine/install/ubuntu/) 处理冲突包，不要在有业务容器的机器上直接卸载运行时。

添加官方软件源：

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<'EOF'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: jammy
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF

sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker run --rm hello-world
```

为了让后续命令无需 `sudo`，将当前用户加入 docker 组并启用新组会话：

```bash
sudo groupadd -f docker
sudo usermod -aG docker "$USER"
newgrp docker
```

docker 组具有宿主机 root 级权限；如果不使用该组，后续每条 Docker 命令加 `sudo` 即可。[Docker 用户组说明](https://docs.docker.com/engine/install/linux-postinstall/)

确认安装：

```bash
docker version
docker compose version
docker buildx version
docker run --rm hello-world
```

项目要求 Compose 2.24.4 或更新版本。使用 `docker compose`，不是旧的 `docker-compose`。

## 3. 准备项目目录

下面假设复制后的目录为 `~/robot-control-compose`，不同路径请修改 `cd`：

```bash
cd ~/robot-control-compose
ls -a
```

应包含 `compose.yaml`、`docker/`、`requirements/`、`packages/`、`src/`、`policy/`、`configs/`、`ros_ws/`、`vendor/`、`tests/`，以及隐藏文件 `.dockerignore` 和 `.env.example`。请完整复制项目，不只复制 Dockerfile。

准备本机配置和持久化目录：

```bash
./scripts/setup-user.sh
export DOCKER_DEFAULT_PLATFORM=linux/amd64

docker compose config --quiet
```

默认配置可以直接运行：

| 配置项 | 默认值 / 含义 |
| --- | --- |
| `configs/rc.mock.json` | mock 机器人，启动 hold 后暂停 |
| `RC_UID` / `RC_GID` | 脚本写入本机普通用户身份；所有业务容器保持一致 |
| `RC_SHM_SIZE` | `512m`，共享内存容量 |
| `RC_MCP_PORT` | `4000`，只绑定宿主 `127.0.0.1` |
| `ROS_DOMAIN_ID` | `83`，用于后续 ROS 模拟器 |
| `ROBOT_CONTROL_MCP_TOKEN` | 默认留空；设置后 HTTP 请求需 Bearer token |

脚本保留 `.env` 的其他设置，不执行其内容；未有 `.env` 时从示例生成。以日常打开项目的普通用户运行，不要加 sudo。脚本只创建缺少的默认目录，不修改已有数据的所有者或权限。复制到另一台机器后重新运行脚本，避免沿用原机器的 UID/GID。使用自定义 Qwen 模型或缓存路径时，应另外以该用户创建对应目录。

此时不用修改 `policy_host`、UDS 路径或共享内存配置。默认 hold 的配置在 `policy/hold/policy.json`。缺少 UID/GID 时 Compose 会要求先运行脚本；缺少宿主挂载目录时启动会报错，避免 Docker 自动生成 root 所有的目录。

## 4. 原生构建 amd64 镜像

在同一终端、项目根目录执行：

```bash
export DOCKER_DEFAULT_PLATFORM=linux/amd64
set -o pipefail
docker compose --progress plain build --pull robot-control policy-hold \
  2>&1 | tee outputs/build-amd64.log
docker compose pull ipc-anchor
```

构建会自动完成：

- 主镜像安装 ROS 2 Humble / Python 3.10 依赖，安装 SDK 和主框架，colcon 编译消息包及 ROS 测试包。
- policy 镜像安装 Python 3.14 / NumPy 2.5.4 / posix-ipc 1.3.2 和 SDK。
- 厂商完整备份保留在项目中；构建只使用 `ros_ws/src/arx5_arm_msg` 副本，不构建或启动厂商驱动。

无需在宿主机执行 `pip install` 或 `colcon build`。第一次耗时主要取决于网络、CPU 和磁盘；失败时保留 `outputs/build-amd64.log`，不要先随意改动锁定版本。

确认三个运行镜像都是 amd64：

```bash
docker image inspect \
  robot-control-compose-rc:local \
  robot-control-compose-policy:local \
  busybox:1.37.0 \
  --format '{{.RepoTags}} {{.Os}}/{{.Architecture}}'
```

三行均应显示 `linux/amd64`。不要加载此前 Mac 上构建的 ARM64 镜像替代这一步。

## 5. 检查依赖和 ROS 消息

```bash
docker run --rm robot-control-compose-rc:local python3 -m pip check
docker run --rm --entrypoint python robot-control-compose-policy:local -m pip check
```

两端都应输出 `No broken requirements found.`。

确认版本与消息导入：

```bash
docker run --rm robot-control-compose-rc:local python3 -c \
  'import os,sys,numpy,importlib.metadata as m; from arx5_arm_msg.msg import RobotCmd,RobotStatus; print(os.environ["ROS_DISTRO"],sys.version.split()[0],numpy.__version__,m.version("posix-ipc")); print("arx5_arm_msg import OK")'

docker run --rm --entrypoint python robot-control-compose-policy:local -c \
  'import sys,sysconfig,numpy,importlib.metadata as m; print(sys.version.split()[0],numpy.__version__,m.version("posix-ipc")); print("Py_GIL_DISABLED:",sysconfig.get_config_var("Py_GIL_DISABLED"))'
```

预期主镜像为 `humble / Python 3.10.x / NumPy 1.26.4 / posix-ipc 1.3.2`，policy 为 `Python 3.14.x / NumPy 2.5.4 / posix-ipc 1.3.2`，`Py_GIL_DISABLED` 为 `0`。

## 6. 第一次启动：mock + hold

```bash
docker compose up -d --no-build
docker compose ps
```

应看到 `ipc-anchor` 和 `robot-control` 在运行，`policy-hold` 为 `healthy`；`docker compose ps -a` 中 `rpc-init` 应为正常退出（0）。主控制器启动后保持暂停。

检查 HTTP 健康状态：

```bash
curl --fail http://127.0.0.1:4000/health
```

正常加载后返回 JSON，其中 `state` 为 `paused`，`policy` 为 `hold`。如果修改过 `RC_MCP_PORT`，同步替换 URL 的端口；如果配置了 token，请加 `Authorization: Bearer ...` 请求头。

进入已经运行的 TUI：

```bash
docker compose attach --detach-keys='ctrl-p,ctrl-q' robot-control
```

在 TUI 命令栏依次试用，等上一条完成后再输入下一条：

```text
status
resume
pause
snapshot
save-config rc
save-config policy
exit
```

- `resume` 开始推理和控制；hold 保持测得的姿态。
- `pause` 暂停新推理并保持当前目标。
- `snapshot` 保存三路 PNG 和完整 state 到宿主 `snapshot/<时间戳>/`。
- `save-config` 保存有效配置到整个目录挂载中的 JSON 文件。
- `exit` 让主控制器 release 并清理后退出。

只想离开 TUI、保留程序运行时，依次按 `Ctrl+P`、`Ctrl+Q`。attach 操作现有进程，不使用 `docker compose exec` 启动第二个控制器。[Compose attach 说明](https://docs.docker.com/reference/cli/docker/compose/attach/)

退出控制器后，停止剩余服务：

```bash
docker compose down
```

`outputs/`、`snapshot/` 和保存过的配置都在宿主项目目录中，正常 `down` 不删除这些内容。RC 与各 policy 使用 `.env` 中相同的 UID/GID，新的快照目录、原子保存后的配置及输出文件都归该宿主用户所有。快照目录仍使用 `0700`、原子配置仍使用 `0600`，所有者可以直接打开和修改，无须设置 `777`。

仅 `rpc-init` 短暂使用 root 初始化内部通信卷，它没有挂载宿主数据目录。各业务容器有自身可写的 `/tmp/robot-control` HOME 和 XDG 缓存，随容器停止清空；需要长期保留的结果应写到已挂载的 outputs 等目录。Qwen 的 HF 缓存单独保存在宿主 `cache/huggingface` 或配置的路径中。

## 7. 使用 headless 命令行

先停止 TUI 控制器，再启动一个交互式 headless 控制器：

```bash
docker compose stop robot-control
docker compose run --rm --service-ports robot-control \
  python3 -m robot_control --headless \
  --config /workspace/configs/rc.mock.json --mcp 0.0.0.0:4000
```

启动后同样保持暂停，可输入 `status`、`resume`、`pause`、`snapshot`、`exit`。交互运行时不要加 `-d`；stdin 关闭会触发退出。结束后执行 `docker compose down`。

## 8. 在这台 amd64 电脑上执行验收

这一步验证目标架构上的依赖、跨 Python/跨容器共享图像和真实 ROS 模拟闭环，使用独立 Compose 项目：

```bash
docker compose -p rc-compose-validation \
  -f compose.yaml -f compose.test.yaml build policy-hold integration

docker compose -p rc-compose-validation \
  -f compose.yaml -f compose.test.yaml run --rm integration
```

当前集成组包含 6 项测试；目标电脑应全部通过，不能直接把已有 ARM64 结果当作 amd64 结果。

继续运行主框架完整回归：

```bash
docker compose -p rc-compose-validation \
  -f compose.yaml -f compose.test.yaml run --rm \
  -e RC_DOCKER_TEST=0 integration \
  python3 -m pytest -p pytest_asyncio.plugin -q -rs
```

该组的本地测试 host 使用 Python 3.10；真正的 Python 3.14 边界由上面的独立集成组验证。部分旧 Conda/外部模型用例按迁移范围显式跳过，原因会显示在汇总中。原 ARM64 验收结果及范围见 [verification.md](verification.md)。

清理验收项目：

```bash
docker compose -p rc-compose-validation \
  -f compose.yaml -f compose.test.yaml down -v
```

## 9. 使用独立 ROS 模拟器

先停止默认 mock 栈，再用同一项目的 ROS 覆盖配置启动：

```bash
docker compose down

docker compose --profile ros-sim \
  -f compose.yaml -f compose.ros-sim.yaml up -d --no-build

docker compose --profile ros-sim \
  -f compose.yaml -f compose.ros-sim.yaml ps

docker compose attach robot-control
```

这次多一个 `ros-sim` 容器，主控制器通过真实 `rclpy` 和 DDS 读写模拟器。输入 `resume` 后检查动作消费数增长，再执行 `pause`、`snapshot`、`exit`。

使用相同的 profile 和文件组合停止，保证模拟器也退出：

```bash
docker compose --profile ros-sim \
  -f compose.yaml -f compose.ros-sim.yaml down
```

## 10. 真机 ACOne 的入口

先完成前面的 mock 和 ROS 模拟器检查。真机配置在 `configs/rc.acone.json`；按现场核对消息类型、反馈/命令/相机话题、限幅、复位姿态和 ROS 域。项目不会自动启动 `vendor/` 中的硬件驱动。

确认实际驱动已运行、配置正确后，使用 Linux host-network 覆盖配置。例如实际 ROS 域为 0：

```bash
ROS_DOMAIN_ID=0 docker compose \
  -f compose.yaml -f compose.acone.yaml up -d --no-build

docker compose attach robot-control
```

这里显式指定 `ROS_DOMAIN_ID=0` 是因为复制 `.env.example` 后默认值为 83；请将 0 改为实际驱动的域。真机覆盖配置中的 MCP 默认监听宿主 `127.0.0.1:4000`，不使用 `RC_MCP_PORT` 的 bridge 端口映射。

加载后仍保持暂停。真实硬件的方向、单位、急停和失联行为需要现场验收；此前模拟器通过不替代真机验收。

停止真机控制栈：

```bash
ROS_DOMAIN_ID=0 docker compose -f compose.yaml -f compose.acone.yaml down
```

## 11. 更新和常用排查

修改 Python 框架或 SDK 源码后，停止当前栈再重新构建：

```bash
docker compose down
./scripts/setup-user.sh
docker compose --progress plain build robot-control policy-hold
docker compose up -d --no-build --force-recreate
```

从旧版升级身份设置时必须重新创建容器，单纯 restart 不生效。所有启停命令保留当前使用的 Qwen、LLM、ROS 或真机 `-f` 组合；LLM 同时保留 `.env` 与 `.env.llm`。新设置不会迁移旧数据权限。

如果当前运行的是 ROS 模拟器或真机，停止时使用相应的文件组合，重新启动时也使用该组合。policy 目录与配置是挂载的；源码修改仍需显式重载，已经运行的模块不会自动替换。冷重载成功后需要再次 `resume`。

常用检查：

```bash
docker compose ps -a
docker compose logs --tail=100 policy-hold
docker compose logs --tail=100 robot-control
docker system df
```

| 现象 | 处理 |
| --- | --- |
| `permission denied /var/run/docker.sock` | 检查 docker 组是否已在当前会话生效，或使用 sudo；不把 socket 改为全员可写 |
| 拉取镜像、apt、pip 超时 | 根据失败阶段检查目标机对应网络/代理；保留构建日志，修复网络后重跑同一 build 命令 |
| `exec format error` 或架构提示 | 用第 4 步检查三个镜像均为 `linux/amd64`；重新原生构建/拉取 |
| `4000` 端口被占用 | 修改 `.env` 的 `RC_MCP_PORT`，重新创建默认栈；真机 host-network 模式则修改 `configs/rc.acone.json` 中的 MCP 端口 |
| 提示先运行 setup-user.sh 或挂载目录不存在 | 以普通用户运行 `./scripts/setup-user.sh`；自定义模型/缓存路径需另行创建；不要依赖 Docker 自动创建宿主目录 |
| 新文件仍归 root 或出现 socket/shared-memory Permission denied | 停止整个栈后检查所有业务服务的 UID/GID 相同，并重新创建容器；不要只修改 RC 或只 restart |
| policy 不 healthy | 查看 policy 日志，确认整个 `policy/` 目录被复制且 manifest 为 schema 2 |
| `camera on` 报“相机协议初始化失败：division by zero” | 分离启动时 PTY 的行列数可能尚为 0。更新后重新构建并创建主容器；初始化会回退到 10×20 像素单元格并缓存，沿用配置的 Kitty/Sixel 协议，attach 后即可开启预览。所用终端仍需支持该协议；行列数正常时继续做能力检测 |
| Ghostty 中 camera 为 connected、snapshot 正常但预览为空 | 使用 kitty 协议，确认主容器有 `COLORTERM=truecolor`。缺少真彩色标识会让 Textual 降色并改变图像 ID；更新 Compose 后需重新创建主容器，单纯 restart 不会更新环境变量 |
| 主控制器 fault | 先看控制器日志；确认配置路径、policy host、ROS 反馈和相机数据，排查后显式重载 |
| 断连后旧读者无法确认 | 停止整个对应 Compose 栈再重建，借助 IPC 命名空间销毁回收冻结帧；不单独重启 policy 来绕过证明 |
| 保存配置或快照后宿主无法编辑 | 停止服务后查看目标文件所有权，对具体文件使用 sudo 编辑或 chown |

进一步的多 policy 同容器/跨容器配置见 [policy-authoring.md](policy-authoring.md)。

## 12. 启用 Qwen single / two-stage

完成默认 hold 构建验收后，按 [Qwen Docker 部署教程](qwen35-policy.md) 安装 NVIDIA Container Toolkit、准备权重和 HF 缓存。两个策略只构建一个 Python 3.11 CUDA 镜像，分别运行独立容器；主框架和 hold 的环境保持不变。

使用 `compose.qwen.yaml` 添加两个 host；与 `compose.acone.yaml` 或 `compose.ros-sim.yaml` 叠加时，后续启动、attach 和停止都保留同样的文件组合。默认 `docker compose up -d` 仍只启动 hold，不要求 GPU。


## TUI 面板与日志调整（2026-10-26）

按住 camera 上边框上下拖动可调整高度（最小4行），窗口缩小时保留日志和命令区。monitor 每列为8字符，内容全部显示时自动隐藏横向滚动条。日志按 UTC+8 显示日期与毫秒，界面省略时区后缀；保存的 ISO 8601 时间仍含 `+08:00`。TUI 分别为时间、四字符级别标签和 RC/MCP/policy 来源着色。JSONL 的 time 字段同步改为字符串，外部日志消费者需按 ISO 8601 解析。更新后重新构建并创建 robot-control 容器。细节见 [日志说明](structured-logs.md)。
