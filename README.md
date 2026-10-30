# robot-control-compose

默认采用依赖镜像＋只读源码挂载。修改 RC、SDK、策略实现或 Qwen vendor/补丁后，按范围重新创建运行组或 switch 即可，无需 build；第三方依赖、系统环境和 ROS 消息变化仍需重建。首次迁移步骤及生效范围见 [部署准备](docs/setup.md#依赖镜像与只读源码)。

独立的 Docker 机器人控制框架。RC 使用 ROS 2 Humble / Python 3.10，hold 和 example 使用 Python 3.14；两个 Qwen 使用相同 Python 3.11 镜像，各自独占容器、执行进程、context 与 socket。SDK 只依赖 NumPy 和 posix-ipc，不依赖 ROS、TUI 或模型框架。

## 准备与运行

本版配置为 **schema 3**，不兼容旧 host/socket 表与根级策略 Compose。支持范围是本地 Linux Docker 部署；其他环境的测试证据单独记录。

```bash
# 只准备所需策略镜像，不启动机器人或策略
./setup.sh --policies hold
./scripts/compose.sh up -d
./scripts/compose.sh attach robot-control
```

基础 Compose 只有 `robot-control` 与 `ipc-anchor`。RC 先处于空策略状态，连接机器人后读取 `startup_policy`，通过生命周期线程创建策略容器；`null` 保持空策略。默认策略失败不会启动备用 hold，管理命令仍可使用。

```text
policies
help hold
switch hold
resume
pause
switch hold --reset
exit
```

`switch` 默认保持当前位置，只有 `--reset` 显式复位。每次切换和冷重载都先清理整个旧运行组，包括新旧链的交集成员，再创建新容器。成功后保持暂停，需要显式 `resume`。新组失败后停在空策略故障状态。

```bash
# 先关闭 RC，再回收动态策略与外部资源，最后关闭基础设施
./scripts/compose.sh down
```

使用项目提供的 down 入口；基础 `docker compose down` 无法独自覆盖动态策略 project。清理无法证明时不会关闭 anchor，也不会启动新策略。`BLOCKED_INFRA` 表示 RC 与 anchor 的 IPC 绑定不再一致，应先完成收尾，再一起重建两者。

## 插件目录

每个可执行策略提供自己的目录：

```text
policy/my_policy/
  policy.yaml
  main.py
  compose.yaml        # 恰好一个服务 policy，明确 image
  deployment.env      # 可选，本机部署参数
  Dockerfile          # 可选，准备镜像时使用
```

目录名是策略来源身份，格式 `[a-z0-9][a-z0-9_-]{0,47}`；manifest 的 `id` 可省略，存在时必须同名。运行实例的 socket 自动派生为 `/run/robot-control/<instance_id>.sock`，实例名最长 64 字符。正式 manifest 没有 `.example` 也能发现；发现和帮助不会导入策略 Python。

新增目录后运行 `./setup.sh --policies my_policy` 准备镜像，再 `switch my_policy`。无须修改 RC、setup 名单或基础 Compose。部署、模型和供应商配置都在策略目录内；运行切换不构建镜像、不安装依赖、不下载模型。

原生 chain 只是成员列表，自身不增加容器；`[a,b,a]` 展开为 `a-0 → b → a-1`，创建三个独立容器、host、执行进程和 context。只有重复策略编号，单例保持原名；编号按同一策略在当前链中的出现顺序从 0 开始。多个实例使用原策略目录和同一镜像，容器内源码路径保持 `/workspace/policy/a`。生成的实例名冲突时，在停止旧组前拒绝应用。SDK 内部 `PolicyChain` 仍是单 worker 内模块组合。

命令与监控按实例注册，例如 `policy a-0 <command>`、`a-0-<command>`、`monitor-a-0` 和 `monitor-a-1`。`a-0` 发起的配置重载仅改变自身参数，仍通过整条链冷重建生效，其他实例保留当前配置。

`save-config a-0`（兼容 `save-config policy a-0`）将该实例配置保存到原目录 `policy/a/`，落盘 `id` 仍为 `a`；当前 `a-1` 不变。同时存在多个 a 实例时，`save-config a` 会报错并要求指定实例。多个实例依次保存到同一个来源文件时，最后一次成功保存成为下次加载的默认配置。链 YAML 和编辑器保存仍记录原策略顺序 `[a,b,a]`；显式 switch 或重新应用链会从来源默认配置重新生成实例。

## 配置与权限

RC/policy 主配置现用 YAML，保存保留注释和顺序；旧 JSON 主配置需手动迁移，见 [迁移说明](docs/setup.md#yaml-主配置与手动迁移)。model/provider JSON 和通信格式保持不变。

主配置 `.yaml.example`、业务配置 `.json.example` 和 `deployment.env.example` 用于生成缺失配置。本机 YAML/JSON、密钥、部署 env、`.setup` 和运行产物不提交。setup 保留现有配置值并备份变更；不会把未知模型格式解释为 Qwen。

RC 持有 Docker daemon 管理权限，用于管理同一 daemon 下的兄弟容器。setup 探测 Unix socket 路径与 GID；不假定固定组号、不修改 socket 为全员可写。策略不会得到 Docker socket 或 host PID namespace。anchor 仅初始化通信卷，然后降权；常驻进程使用宿主用户 UID/GID。

图像通过只读 POSIX 共享内存传输。断连不是读者退出证明；容器退出只能证明本地读者结束，容器外模型服务必须独立确认释放。RC 重启会先核对旧容器和未完成外部 reader 账本。

## MCP 等待 policy 结果

当前 policy 或链成员使用 SDK 0.10 的 `runtime.finish()` 后，MCP 可以等待任务结果：

```text
mcp take
mcp return_and_wait {"timeout_s":30}
mcp get_policy_result
mcp get_policy_result <operation_id>
```

`return_and_wait` 默认不超时，`timeout_s: 0` 只提交并立即返回回执。交接成功后，普通动作返回不会结束等待；只有显式完成/终止、人工干预或故障才结束该任务。超时、取消 MCP 调用和客户端断连只结束本次等待，任务仍运行，结束后仍交还 MCP；用只读 `get_policy_result` 查询同一回执。结果不被读取消费，只保留在本次 RC 运行的有界历史中。

显式回调的回执状态为 `succeeded`，`result.outcome` 为 `completed` 或 `terminated`，并包含 `message` 和来源 policy；`terminated` 是主动终止报告，不表示业务成功。普通暂停或人工干预返回 `cancelled`、`reason="人工接管"`，取消自动交还资格。policy 故障返回 `failed` 并交还 MCP，仍按既有规则清理和保持故障保护。没有等待任务时，`finish` 只暂停并保持 policy 所有权。

原有 `return(wait=true)` 仍只等待交接完成。新增能力使用前同步升级 RC 和使用回调的 policy 镜像；本轮没有为 `llm_direction` 或其他业务策略自动加入完成判断。回调规则见 [策略作者规范](docs/policy-authoring.md#任务完成与终止)。

## 文档与验证

- [部署与配置准备](docs/setup.md)
- [Ubuntu 22.04 从零部署](docs/ubuntu22-amd64-from-scratch.md)
- [策略作者规范](docs/policy-authoring.md)
- [Python 3.14 本地范例策略](policy/example/README.md)
- [Qwen 策略](docs/qwen35-policy.md)
- [LLM 离散动作策略](docs/llm-direction-policy.md)
- [LLM 异步失败检测策略](docs/llm-failure-policy.md)：原样透传动作，每 60 个实际执行步用当前与历史图像检查失败，通过 MCP 等待回执返回。
- [策略监控](docs/policy-monitor.md)
- [连续录制与推理追踪](docs/recording.md)：`record on/off/status`、三路 30 Hz RGB、状态和成功发布动作、逐实例推理与独立压测。
- [测试与验收边界](docs/verification.md)

只构建 `ros_ws` 下的消息包，厂商备份原样保留；项目不自动启动硬件驱动。mock、真实 Docker、ROS 仿真与真机验证分别记录，模拟测试通过不代表真机验收。

## 全局状态扩展

RC/SDK 0.11 支持所有策略共享的 JSON 字典：`runtime.globals` / `controller.globals`，终端 `show global`、`set global.<key> <JSON>`、`unset global.<key>`。RC 统一提交并在退出时保存至 `global.env`，policy 切换保留内容。见 [全局共享状态](docs/global-state.md)。
