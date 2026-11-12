# robot-control-compose

默认采用依赖镜像＋只读源码挂载。修改 RC、SDK、策略实现或 Qwen vendor/补丁后，按范围重新创建运行组或 policy switch 即可，无需 build；第三方依赖、系统环境和 ROS 消息变化仍需重建。首次迁移步骤及生效范围见 [部署准备](docs/setup.md#依赖镜像与只读源码)。

独立的 Docker 机器人控制框架。RC 和 mock connector 使用无 ROS 的 Python 3.10 镜像；ACOne connector 独占 ROS 2 Humble / Python 3.10 容器。hold 和 example 使用 Python 3.14；两个 Qwen 使用相同 Python 3.11 镜像，各自独占容器、执行进程、context 与 socket。SDK 只依赖 NumPy 和 posix-ipc，不依赖 ROS、TUI 或模型框架。

connector 只查询状态和 FIFO 发送设备消息；RC 管理频率、限速和控制权，收到完整发布确认后消费目标。图像按 RC 请求写入 POSIX 共享内存，通信通道只传描述符。配置迁移和部署方式见 [connector 公约与使用说明](docs/connectors.md)。

[LIFT2 connector](docs/lift2.md) 使用独立 ROS 1 Noetic / CPython 3.10 镜像，支持双臂及夹爪关节控制、末端状态查询、三路 RGB 和现有预览/录制；`epos` 控制明确拒绝。可通过 `./setup.sh --policies example --connectors mock,lift2` 构建，填写实际 ROS 网络参数后用 `connector switch lift2` 切换。

## 准备与运行

本版配置为 **schema 3**，不兼容旧 host/socket 表与根级策略 Compose。支持范围是本地 Linux Docker 部署；其他环境的测试证据单独记录。

```bash
# 首次为宿主 python3 安装锁定的 YAML 依赖
python3 -m pip install --require-hashes --only-binary=:all: -r requirements/setup.lock
# 只准备所需策略镜像，不启动机器人或策略
./setup.sh --policies hold
./scripts/up.sh
./scripts/attach.sh
```

常用入口为 `scripts/up.sh [Compose 覆盖文件...]`、`scripts/attach.sh` 和 `scripts/down.sh`。按目标构建可用 `scripts/build.sh rc`、`scripts/build.sh ipc-anchor`、`scripts/build.sh webui`、`scripts/build.sh <策略来源>`；connector 使用明确前缀，例如 `scripts/build.sh connector:mock` 或 `scripts/build.sh connector:acone`。完整参数仍可通过 `scripts/compose.sh` 使用。

基础 Compose 只有 `robot-control` 与 `ipc-anchor`。RC 先处于空策略状态，连接机器人后读取 `startup_policy`，通过生命周期线程创建策略容器；`null` 保持空策略。默认策略失败不会启动备用 hold，管理命令仍可使用。

四个快捷入口都可从其他工作目录调用，沿用 `SETUP_PYTHON`；使用前先完成 setup：

```bash
# 自动加入 compose.yaml，其余文件按参数顺序追加 -f，执行 up -d
./scripts/up.sh compose.acone.yaml compose.webui.yaml
# 自动寻找构建定义，只构建指定服务；rc 是 robot-control 的别名
./scripts/build.sh rc
./scripts/build.sh webui
./scripts/build.sh example  # 策略使用来源目录名，读取自己的 deployment.env
# 固定连接 robot-control，不接收参数
./scripts/attach.sh
# 无需重复传覆盖文件，清理当前部署的全部容器及动态策略
./scripts/down.sh
```

`up.sh` 只接收 Compose 文件路径（相对仓库根目录或绝对路径），无参数时只用基础文件。`build.sh` 恰好接收一个服务名或策略来源名，在基础文件、根目录 `compose*.yaml` / `compose*.yml` 和对应策略的 `compose.yaml` 中查找；找不到构建定义或多个覆盖文件定义同一目标时会报错。`down.sh` 复用原有退出证明与资源回收流程，并通过 `--remove-orphans` 清掉 WebUI、ROS 仿真等附加容器；范围仅限当前部署，不删除镜像或命名卷。需要额外 Compose 选项时继续使用 `scripts/compose.sh`。

```text
policy list
policy help hold
policy switch hold
resume
pause
policy switch hold --reset
exit
```

`policy switch` 默认保持当前位置，只有 `--reset` 显式复位。显式切换和 `reload_all_policy` 先清理整个旧运行组，再创建新容器。`reload_policy` 只重建命令目标实例。成功后均保持暂停，使用 `resume` 继续。局部初始化失败时，健康成员驻留并等待恢复。

`reinitialize_policy` 在目标原容器、host 和执行进程中先释放旧 context，再用当前配置或冷字段补丁初始化新 context。自链可用 `policy example-1 reinitialize` 体验。状态栏显示最近一次动作请求返回时的裁剪与缓存帧数，`logging.level: TRACE` 启用逐条摘要。在包含 llm_failure 的链上使用 `monitor llm_failure` 查看本轮请求耗时、token、失败次数和在途数。

```bash
# 先关闭 RC，再回收动态策略与外部资源，最后关闭基础设施
./scripts/down.sh
```

使用项目提供的 down 入口；基础 `docker compose down` 无法独自覆盖动态策略 project。清理无法证明时不会关闭 anchor，也不会启动新策略。`BLOCKED_INFRA` 表示 RC 与 anchor 的 IPC 绑定不再一致，应先完成收尾，再一起重建两者。

## 插件目录

每个可执行策略提供自己的目录：

```text
policy/my_policy/
  policy.yaml
  main.py
  src/my_policy/      # 本策略独立持有的业务实现
  vendor/             # 可选，本策略专用的模型源码与来源记录
  scripts/            # 可选，本策略专用工具
  docs/               # 可选，本策略专用资料
  compose.yaml        # 恰好一个服务 policy，明确 image
  deployment.env      # 可选，本机部署参数
  Dockerfile          # 可选，准备镜像时使用
```

目录名是策略来源身份，格式 `[a-z0-9][a-z0-9_-]{0,47}`；manifest 的 `id` 可省略，存在时必须同名。运行实例的 socket 自动派生为 `/run/robot-control/<instance_id>.sock`，实例名最长 64 字符。正式 manifest 没有 `.example` 也能发现；发现和帮助不会导入策略 Python。

新增目录后运行 `./setup.sh --policies my_policy` 准备镜像，再 `policy switch my_policy`。无须修改 RC、setup 名单或基础 Compose。部署、模型和供应商配置都在策略目录内；运行切换不构建镜像、不安装依赖、不下载模型。

每个 policy 的业务代码、模型源码、依赖锁文件、构建文件和专用资料都放在自己的目录中。`packages/` 仅保留公共 `policy-sdk` 和 `connector-sdk`；策略之间不互相导入业务实现，也不通过软链接共用源码。相同依赖镜像仍可复用；实际模型权重和下载缓存继续通过外部目录挂载。项目级验收集中在 `tests/`。

原生 chain 只是成员列表，自身不增加容器；`[a,b,a]` 展开为 `a-0 → b → a-1`，创建三个独立容器、host、执行进程和 context。只有重复策略编号，单例保持原名；编号按同一策略在当前链中的出现顺序从 0 开始。多个实例使用原策略目录和同一镜像，容器内源码路径保持 `/workspace/policy/a`。生成的实例名冲突时，在停止旧组前拒绝应用。SDK 内部 `PolicyChain` 仍是单 worker 内模块组合。

命令与监控按实例注册，例如 `policy a-0 <command>`、`monitor a-0` 和 `monitor a-1`。`a-0` 发起的重载仅修改自身参数。命令声明的 `effect` 决定重建 `a-0` 还是整条链。

`policy a-0 config save` 将该实例配置保存到原目录 `policy/a/`，落盘 `id` 仍为 `a`；当前 `a-1` 不变。同时存在多个 a 实例时，`policy a config save` 会报错并要求指定实例。多个实例依次保存到同一个来源文件时，最后一次成功保存成为下次加载的默认配置。链 YAML 和编辑器保存仍记录原策略顺序 `[a,b,a]`；显式 policy switch 或重新应用链会从来源默认配置重新生成实例。

## 配置与权限

完整语法见 [统一命令树](docs/commands.md)。RC 使用 `config rc show/set/save` 编辑冷配置草稿；实例使用 `policy <实例> config show/set/save`，由策略的 configure 接口解析设置项和值。普通修改双端确认后生效，reset/reinitialize 完成后保持暂停。

RC/policy 主配置现用 YAML，保存保留注释和顺序；旧 JSON 主配置需手动迁移，见 [迁移说明](docs/setup.md#yaml-主配置与手动迁移)。两个 LLM 策略各自通过 `params.provider_config_path` 读取供应商 YAML。在 TUI 中用 `policy <实例> switch-provider <文件路径>` 切换，成功后只重建该实例并暂停整链。目标初始化失败可修正文件后执行 `policy <实例> retry`。

RC/SDK 0.17 支持 [机器人 connector 热切换](docs/connector-switching.md)：`connector switch <命名配置>` 退出旧 connector 并创建新 connector，RC 与策略容器持续运行，完成后暂停。每个策略的 `execution.on_connector_switch` 默认 `reinitialize_policy`，可热改为 `reset` 保留原 context；混合链按实例执行，录制自动分会话。

主配置和 LLM 供应商配置使用 `.yaml.example`，模型配置使用 `.json.example`，部署环境使用 `deployment.env.example`。setup 从模板生成缺失文件，保留已有供应商文件。本机 YAML/JSON、密钥、部署 env、`.setup` 和运行产物不提交。

RC 持有 Docker daemon 管理权限，用于管理同一 daemon 下的兄弟容器。setup 探测 Unix socket 路径与 GID；不假定固定组号、不修改 socket 为全员可写。策略不会得到 Docker socket 或 host PID namespace。anchor 仅初始化通信卷，然后降权；常驻进程使用宿主用户 UID/GID。

图像通过只读 POSIX 共享内存传输。断连不是读者退出证明；容器退出只能证明本地读者结束，容器外模型服务必须独立确认释放。RC 重启会先核对旧容器和未完成外部 reader 账本。

## MCP 等待 policy 结果

MCP 的链列表应用/保存、录制、策略命令与定向帮助见 [MCP 管理与控制](docs/mcp.md)。录制开关无需接管；其余新增写操作先 `take`，完成后保留 MCP 控制权并暂停 policy。

当前 policy 或链成员使用 SDK 0.10 的 `runtime.finish()` 后，MCP 可以等待任务结果：

```text
mcp take
mcp return_and_wait {"timeout_s":30}
mcp get_policy_result
mcp get_policy_result {"operation_id":"<operation_id>"}
```

`return_and_wait` 默认不超时，`timeout_s: 0` 只提交并立即返回回执。交接成功后，普通动作返回不会结束等待；只有显式完成/终止、人工干预或故障才结束该任务。超时、取消 MCP 调用和客户端断连只结束本次等待，任务仍运行，结束后仍交还 MCP；用只读 `get_policy_result` 查询同一回执。结果不被读取消费，只保留在本次 RC 运行的有界历史中。

显式回调的回执状态为 `succeeded`，`result.outcome` 为 `completed` 或 `terminated`，并包含 `message` 和来源 policy；`terminated` 是主动终止报告，不表示业务成功。普通暂停或人工干预返回 `cancelled`、`reason="人工接管"`，取消自动交还资格。policy 故障返回 `failed` 并交还 MCP，仍按既有规则清理和保持故障保护。没有等待任务时，`finish` 只暂停并保持 policy 所有权。

原有 `return(wait=true)` 仍只等待交接完成。新增能力使用前同步升级 RC 和使用回调的 policy 镜像；本轮没有为 `llm_direction` 或其他业务策略自动加入完成判断。回调规则见 [策略作者规范](docs/policy-authoring.md#任务完成与终止)。

## 文档与验证

可选的 [Vue WebUI 三路监控](docs/webui.md) 通过独立 Nginx 容器接入现有 RC：支持三路 640×480 MJPEG、复位和同源 AI 代理的自然语言常用控制。设置 `llm: false` 可按行提交 TUI 命令批次，无需 AI 配置。API 地址与 Key 保存在服务端私有配置，兼容不支持 CORS 的接口。使用 `compose.webui.yaml` 启用，运行时配置不进入镜像。

日常 RC 回归只需 `python3 scripts/test_scope.py`：用 mock 双臂、真实 worker 及两个独立的 example 链成员覆盖公共能力，不需要业务模型。改某个策略时加 `--policy <来源名>`；改了 RC 与策略之间的公共接口时用 `--interface` 运行完整套件。操作示例见 [example 使用说明](policy/example/README.md)，范围判定见 [验证文档](docs/verification.md#按改动选择测试)。

- [部署与配置准备](docs/setup.md)
- [Ubuntu 22.04 从零部署](docs/ubuntu22-amd64-from-scratch.md)
- [策略作者规范](docs/policy-authoring.md)
- [Python 3.14 本地范例策略](policy/example/README.md)
- [Qwen 策略](policy/qwen35_single/README.md)
- [Qwen 视觉历史策略](policy/qwen35_history/README.md)
- [InternVLA MaaS 网络策略](policy/internvla_maas/README.md)：独立 Python 3.11 轻量容器，复用 Qwen 单阶段动作处理，通过 WebSocket 获取 action chunk。
- [LLM 离散动作策略](policy/llm_direction/README.md)
- [LLM 异步失败检测策略](policy/llm_failure/README.md)：原样透传动作，每 60 个实际执行步用当前与历史图像检查失败，通过 MCP 等待回执返回。
- [策略监控](docs/policy-monitor.md)
- [连续录制与推理追踪](docs/recording.md)：`record start/stop/status`、三路 30 Hz RGB、状态和成功发布动作、逐实例推理与独立压测。
- [测试与验收边界](docs/verification.md)

只构建 `ros_ws` 下的消息包，厂商备份原样保留；项目不自动启动硬件驱动。mock、真实 Docker、ROS 仿真与真机验证分别记录，模拟测试通过不代表真机验收。

## 全局状态扩展

RC/SDK 0.11 支持所有策略共享的 JSON 字典：`runtime.globals` / `controller.globals`，终端 `global show`、`global set <key> <JSON>`、`global remove <key>`。RC 统一提交并在退出时保存至 `global.env`，policy 切换保留内容。见 [全局共享状态](docs/global-state.md)。

统一 TUI/headless 命令、RC 冷草稿和策略 configure 入口见 [命令树](docs/commands.md)。RC/SDK 0.15.0 新增本地历史虚影、显式确认的 Tab 候选菜单、参数提示和容器内路径补全；九个内置来源的示例清单均提供静态提示，schema 仍为 3。
