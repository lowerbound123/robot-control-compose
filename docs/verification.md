# Schema 3 动态策略容器验收

## Qwen3.5 视觉历史策略（2026-11-06）

新增独立 `qwen35_history`，使用现有每动作回调、消费计数和生命周期，不修改RC/SDK接口或旧Qwen。实现及部署边界见 [策略说明](qwen35-history-policy.md)。

- 完整所选范围 `python3 scripts/test_scope.py --policy qwen35_history`：**120 passed、13 skipped**，55.88秒。包含example基线、同步/异步X步调度、历史、正式入口、真实本地host/worker与POSIX图像租约、任务热改和实例冷重载。宿主没有Torch/Transformers，11个CPU后端用例和1个图测试模块在这里跳过；另1项需要ROS。
- 独立Python3.11 / Torch2.10 / torchvision0.25 / Transformers5.2 CPU环境：后端、图回退、调度、配置和部署静态套件 **53 passed、1 skipped**，2.43秒。覆盖实际张量处理、三路输入、历史量化、32↔14维映射、delta/abs与quantile归一化、FP32专家投影、Euler更新、非有限图差异回退及源补丁delta-rule数值。唯一跳过项为真实NVIDIA CUDA捕获回放。
- 两个自审回归先失败再修复：checkpoint loader隐式依赖`packaging.version`导入顺序；prefix graph接受NaN参考差值。新补丁的三处CPU零张量分配改为在输入设备分配，meta设备测试先失败后通过，手算递推数值保持一致。
- 实际`dagger_clean_win`的config/train_config在本地成功反序列化，远端统计字段核对为左右各6+1维。只读取元数据，没有加载真实权重。
- 新增Python文件语法检查、来源SHA-256校验及`git diff --check`通过；RC、SDK、旧Qwen包/vendor/manifest/依赖锁的diff为空。本轮单智能体实施与自审，未构建或运行Docker、GPU模型、ROS或机器人硬件；跳过不计通过。

证据：`/tmp/qwen35-history-scope-final.log`、`/tmp/qwen35-history-cpu-final.log`、`/tmp/qwen35-history-review-red.log`、`/tmp/qwen35-history-review-green.log`、`/tmp/qwen35-history-graphs-red.log`、`/tmp/qwen35-history-graphs-green.log`。

## 按改动选择测试

在已安装 RC 与测试依赖的 Python 环境中，从仓库根目录运行。入口会为测试及其子进程设置当前仓库的源码路径；不安装依赖、不构建模型镜像。范围由改动的实际语义决定，不用文件名推断接口是否改变。

| 改动 | 命令 | 验证范围 |
| --- | --- | --- |
| RC 内部、TUI、录制等改动，策略看到的契约不变；或只改 example | `python3 scripts/test_scope.py` | 运行完整 example 基线；具体模块改动另补对应 RC 单测。链只启动两个 example，其他业务策略不参与。 |
| 只改某个策略的实现、manifest、镜像或依赖 | `python3 scripts/test_scope.py --policy llm_failure` | example 基线加该来源的策略套件。支持 hold、llm_direction、llm_failure、pi05、qwen35_single、qwen35_twostage、qwen35_history、internvla_maas；可同时列多个来源。 |
| 两个策略共用的实现或依赖变化 | 例如 `python3 scripts/test_scope.py --policy qwen35_single qwen35_twostage` | 单选 Qwen 测该入口、部署和 worker；两个都选时再加共享配置、调度、后端和 CUDA 用例。变更 llm-provider-config 时选 llm_direction 和 llm_failure。 |
| RC↔policy/SDK 公共契约改变 | `python3 scripts/test_scope.py --interface` | 整个 pytest 套件，含 RC、SDK、example、业务策略和跨策略对照；按所需环境追加 Docker/模型兼容性验证。 |

所有命令都可加 `--collect-only` 先看范围。无参数的 `python3 -m pytest` 仍表示全套。新增策略时，将该来源的专属测试登记在 [test_scope.py](../scripts/test_scope.py)。仅改 RC 实现并不自动升级范围；如果回调签名、action/state/images 形状、命令/manifest、热改确认、退出/租约证明或 MCP 等待 policy 结果的语义变了，即使函数名没变，也属于接口变更。策略专属模型输出、权重、CUDA 和供应商协议由对应策略验证，不能用 example 推断。

**example 基线的证据**来自 [策略回调](../tests/test_example_policy.py)、[真实 worker 和重复实例](../tests/test_example_worker.py)、[RC/TUI/MCP 闭环](../tests/test_example_controller.py)、[独立外部读者](../tests/test_example_reader.py) 和 [ROS 仿真](../tests/test_example_ros.py)。它覆盖 sync/async、qpos/epos/mixed、慢推理缓存、观测/图像、监控、录制/快照、姿态存取、配置、全局状态、暂停/完成/终止/故障与控制权。RC 故障注入、部分发布、超时清理等专项仍保留原有单测。

容器生命周期、IPC 或部署改动时，在默认基线之外运行 `./setup.sh --policies example` 和 `python3 tests/example_docker_smoke.py --evidence-dir /tmp/example-evidence`。该脚本不需要 hold 或其他策略。ROS 缺失时相应测试会跳过；在已 source Humble/ros_ws 的环境用同一入口即可执行。也可以直接用 RC 测试镜像跑包含 ROS 仿真的 example 基线，无须在宿主安装 ROS：

```bash
docker build -f docker/rc.Dockerfile --target test -t rc-example-tests:local .
docker run --rm --network none --shm-size=512m --user "$(id -u):$(id -g)" \
  --workdir /workspace --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  -e HOME=/tmp -e 'PYTEST_ADDOPTS=-p no:cacheprovider' \
  -e PYTHONPATH=/workspace/src:/workspace/packages/policy-sdk/src \
  -e 'RC_SOURCE_MODULES={"robot_control":"/workspace/src","rc_policy_sdk":"/workspace/packages/policy-sdk/src"}' \
  rc-example-tests:local python3 scripts/test_scope.py
```

### 0.13 同进程重初始化、实例与整链重载

`tests/example_docker_smoke.py` 的 `result.json.same_process` 给出两次原进程重初始化的前后对照，分别使用空补丁和冷字段补丁。目标的容器 ID、host、runtime、PID 和模块 ID 应一致，其 context ID 每次改变，目标配置和链版本各递增一次。另一实例的容器、进程、context 和版本均应一致。宿主接口套件另外用失效的临时 main.py 验证同进程路径只使用已加载模块。

验收脚本在这两次命令期间拦截 Docker 调用，任何调用都会令用例失败。随后模拟 SDK 已完成初始化、但 RC 收到错误确认。字段 `retry_after_reinitialize` 记录故障与手动恢复时的身份，核对故障阶段保留全部容器、配置维持在已提交版本，冷重试只替换指定实例。

2026-11-02 验收中，宿主完整接口套件 1224 项通过、18 项跳过，Linux / ROS 2 Humble 完整接口套件 1111 项通过、19 项跳过。真实 Docker 自链使用 Python 3.10.12 的 RC 和 Python 3.14.9 的 example，目标配置版本从 4 到 5 再到 6，容器、host、PID 9 和模块身份保持一致。ROS 仿真闭环和本地 Chat / Decisions HTTP 替身均已验证，真实供应商、GPU 模型和机器人硬件未参与本轮验收。

`tests/example_docker_smoke.py` 在同一条 `example → example` 链上对照两种 effect。`result.json` 的 `reload_identities` 记录每一步的容器 ID、host 身份、执行进程身份和配置版本。局部重载只应更换目标实例，整链重载应更换两者。目标创建回执丢失以及下一次 host 就绪失败时，脚本检查只剩原来的健康成员，并通过 `retry-reload example-1` 恢复。

检查 `cleanup_exit` 可核对最后的 RC 退出码。故障注入可能在新容器安装信号处理前将其停止，脚本要求强制退出记录准确对应这一次创建尝试，其余成员正常释放。退出后还会核对图像池、容器和共享卷。

## MCP 策略管理验收

`tests/test_mcp_management.py` 使用正式 example 和真实 host/worker 验证只读能力发现、HTTP/本地命令、严格 JSON 参数、重复实例寻址、链 apply/save 分离、三种 effect、失败实例冷重试、独立录制与等待任务、代次取消及 YAML 原子提交。另覆盖合法的同名 `record` 来源和 `return_and_wait` 策略命令，避免帮助或内部请求路由混淆。完整接口回归运行 `python3 scripts/test_scope.py --interface`。

`tests/example_docker_smoke.py` 增加真实 HTTP MCP 验收。`result.json.mcp_management` 记录链应用、同 PID 重初始化、目标重载、失败后冷重试和整组重建的容器/host/runtime/PID/版本；`recording` 与 `outcome` 核对 policy 运行时独立录制及原等待任务完成。脚本仍只使用 example，最终 `cleanup.json` 验证本次容器和卷为空；既有 lost-create 注入导致的强制退出码单独记录，不伪装成正常清理。

2026-11-03 最终验证：宿主 Python 3.11 完整 `--interface` 为 **1362 passed / 18 skipped / 13 个既有 Pillow 弃用告警**；Humble/Python 3.10 的新管理用例与真实 ROS 模拟器为 **17 passed**；真实 Docker 的 **7 组检查通过**，RC 使用 Python 3.10.12，example 使用 Python 3.14.9。源代码通过 Python 3.10 语法检查。未调用真实模型供应商、GPU 模型或机器人硬件，跳过不计通过。

证据：`/private/tmp/rc-mcp-management-interface-final.log`、`/private/tmp/rc-mcp-management-linux-final.log`、`/private/tmp/rc-mcp-management-docker-final/{result.json,cleanup.json}`。最终资源复核另存 `/private/tmp/rc-mcp-management-cleanup-final.json`。

## InternVLA MaaS 224×224 图像发送（2026-11-05）

按用户确认的方式，在 MaaS 后端发送前用双线性插值等比缩放、居中补黑边，三路输出固定为 RGB `uint8[224,224,3]` ndarray。原始观测、RC/SDK 契约、相机映射和动作逻辑不变。新增 `resize` 分项及 `last_source_image_bytes`，`last_image_bytes` 统计实际发送图像，固定为 451,584 字节。

- 缩放专项 **12 failed → 12 passed**，覆盖横竖图、奇数尺寸、极窄图、非连续数组、只读输入、RGB 通道和已为 224×224 时像素保持；真实本地 WebSocket 验证三路完整 224×224 数据和消息大小。已知时钟确认三路 resize 合计 3 ms 并计入 prepare。
- 受影响的 MaaS 图像、backend、timing、monitor、runtime、worker、部署发现七个文件 **71 passed**，4.65 秒。没有运行全量回归、ROS、Docker 或真实 MaaS/硬件。
- 新增策略依赖 Pillow 12.3.0 及哈希锁，其余依赖版本不变。Python 3.11 环境通过离线、哈希校验的安装 dry-run（5 个包，无需变更）。本次未重建镜像；部署需执行 `./setup.sh --policies internvla_maas` 后冷重载策略。

证据：`/private/tmp/rc-maas-resize-red.log`、`/private/tmp/rc-maas-resize-green.log`、`/private/tmp/rc-maas-resize-targeted.log`、`/private/tmp/rc-maas-resize-lock.log`。

## InternVLA MaaS 策略内分阶段计时（2026-11-05）

在原有请求总延迟和失败次数之外，增加 14 个阶段的平均耗时、最近耗时和样本数，以及图像/请求/响应字节数。覆盖快照提交、网络线程派发、准备、打包、发送、响应等待、解码、校验、服务端自报时间、后台推理、结果就绪等待、结果收集、夹爪后处理和完整回调。复用 SDK 原调度实现及完成时间戳，不修改 RC/SDK 公共接口。

- 新增专项先 **10 failed**，实现后 **10 passed**。已知时钟结合真实本地 WebSocket 核对打包 2 ms、发送 4 ms、等待 50 ms、解码 3 ms，以及实际消息字节数；服务端时间缺失/非法不伪造样本、不拒绝合法动作。
- 受影响的 timing、monitor、backend、runtime、worker 五个 MaaS 文件最终 **58 passed**，4.46 秒。验证就绪后等待 125 ms 的采纳延迟、缓存回调不新增模型请求计数、失败阶段计时、原失败次数与主动取消口径、只读 monitor 和普通 reset 保留统计。
- 新阶段只保存平均值、最近值和计数，不增长历史数组；合并快照满足 SDK 的内建标量、项数和大小限制。分项存在包含关系及不同样本群体，不能直接相加。没有启动全量回归、ROS、Docker、真实 MaaS 或硬件，RC 端计时不在本次范围。

证据：`/private/tmp/rc-maas-detailed-red.log`、`/private/tmp/rc-maas-detailed-green.log`、`/private/tmp/rc-maas-detailed-targeted.log`。

## InternVLA MaaS async 配置与请求监控（2026-11-05）

取消策略专属的 sync/每步回调限制及禁止所有热修改的检查，模板默认 async，支持模式和调用间隔的白名单热修改。新增只读 monitor：平均、中位、上次请求延迟（毫秒）与失败次数；统计当前 context 内实际发送的已结束请求，排除握手和主动取消。

- 按用户要求仅验证受影响的 MaaS 配置、部署发现、monitor、backend、runtime、worker 六个文件：**82 passed**，4.63 秒。没有运行全量回归、example 套件、ROS 或 Docker。
- 精确时钟用例核对奇偶样本中位数、平均值、失败次数去重，以及成功、服务端拒绝、非法动作、断线、超时、主动取消的统计。请求在途时反复读 monitor 不会发请求或等待推理。
- 真实本地 host/worker 验证 monitor 独立响应、共享图像释放、版本确认、sync/async 和调用间隔热修改；运行中的 RC 保留 worker 和已有连接，普通 reset 保留统计。冷重载和故障恢复沿用已有行为。

证据：`/private/tmp/rc-maas-monitor-red.log`、`/private/tmp/rc-maas-monitor-worker-red.log`、`/private/tmp/rc-maas-monitor-targeted-final.log`。中间新增断言忽略了 resume 原有的重连，改为比较热修改前后的会话数量后通过；没有为此修改生产生命周期逻辑。未连接真实 MaaS 服务或硬件。

## InternVLA MaaS ndarray 编码（2026-11-05）

收发切换为用户已验证的 `diagnose_maas_ndarray.py` 编码：字节键 `__ndarray__` / `__npgeneric__`，独立数组副本，4 MiB 入站限制。移除 `msgpack-numpy`，其余锁定依赖版本不变；RC↔SDK 契约未变。

- 编码专项 **64 passed**；独立测试参考的四个函数与成功脚本 AST 一致。新增测试先在旧实现出现 **56 failed、8 passed**，替换后通过。
- 无 `msgpack-numpy` 的 Python 3.11 环境，所选 `--policy internvla_maas` 范围 **207 passed、1 skipped**，71.74 秒；宿主缺 ROS 的跳过不计通过。新 Linux/Python 3.11 测试镜像中策略套件 **124 passed**，0.83 秒。
- 已启动的 MaaS 隔离 Docker 验收 **4 组通过**，RC 退出码 0；example 自链 **7 组通过**，既有故障注入的强制清理如实保留 RC 退出码 1。两套临时部署清理完成。
- ROS 补跑 **1 failed**：只读测试容器未设置可写 HOME，ROS 无法创建 `/root/.ros/log`，未进入策略执行。用户随后要求停止扩展回归并直接提交推送，因此不再补跑。未运行全仓 `--interface`，未连接真实 MaaS 或机器人。

证据：`/private/tmp/rc-maas-ndarray-{codec-red,codec-green,scope,linux,ros}.log`，以及 `/private/tmp/rc-maas-ndarray-docker/`、`/private/tmp/rc-maas-ndarray-example-docker/` 下的结果与清理记录。正式 MaaS 运行镜像和专用测试镜像构建成功。

## InternVLA MaaS 网络策略（2026-11-04）

实现与配置见 [网络策略说明](internvla-maas-policy.md)。本轮没有修改 RC↔SDK 公共契约，也没有修改 Qwen 实现；服务地址、身份和凭据由用户提供。

- 最终 `python3 scripts/test_scope.py --policy internvla_maas`：**127 passed、1 skipped**，33.01 秒。覆盖 example 基线及配置、msgpack-numpy 安全解码、真实 WebSocket、异步补块、夹爪开关、取消/故障、部署发现与真实 host/worker 冷切换。宿主缺少 ROS 的跳过不计通过。
- Python 3.11.17 / Linux 策略测试镜像：**71 passed**，0.70 秒；单独 Humble / Python 3.10 example ROS 仿真：**1 passed**，2.80 秒。
- 新策略隔离 Docker：**4 组检查通过**，RC Python 3.10 → policy Python 3.11，实际动态容器、共享图像、MaaS 二进制请求、冷切换、stop 故障与手动恢复，RC 退出码 0。临时容器、卷和网络已清空。
- example / example 自链隔离 Docker：**7 组检查通过**，包含实例隔离、重初始化/重载/重试、HTTP MCP 和录制；既有 lost-create 故障注入产生的强制清理被正确记录为 RC 退出码 1，验收脚本退出 0，清理检查通过。
- 依赖、构建工具和测试依赖均有固定版本及哈希锁。标准镜像 `robot-control-compose-internvla-maas:local` 构建成功；源码语法与 `git diff --check` 通过。自审补齐空 userinfo 地址拒绝、float32 溢出和缺失可选依赖时的测试收集。

证据：`/private/tmp/rc-internvla-maas-scope-final.log`、`/private/tmp/rc-internvla-maas-linux-final.log`、`/private/tmp/rc-internvla-maas-ros.log`，以及 `/private/tmp/rc-internvla-maas-docker/` 和 `/private/tmp/rc-internvla-maas-example-docker/` 内的结果与清理 JSON。本轮为单智能体实施与自审；Docker 环境是本机 OrbStack Linux/arm64。真实 MaaS 服务、目标部署平台和机器人硬件尚未验收，未调用附件中的地址或凭据。

## 按运行实例名 set 配置（2026-11-04）

`set <实例ID>.<字段路径> <JSON值>` 支持单策略与原生链。唯一来源直接用策略名，重复来源使用 `-0/-1/...`。链内按热字段白名单提交，只更新目标实例及逻辑链版本；其他实例的配置、context 与执行进程不变。受理时捕获运行组、版本与动作代次，避免排队编辑在暂停、接管或切换后落到新实例。单策略的 `set policy.*` 及冷配置路径保持兼容，链内冷参数沿用显式实例重载命令。

按本次用户要求仅执行受影响范围，策略验证使用 example：

- `python3 scripts/test_scope.py`：**76 passed、1 skipped**，53.25 秒。包括具名唯一/重复实例、精确更新、原配置引用与 context、JSON 字符串/null、保存、歧义与冷字段拒绝、控制代次、MCP 和迟到 ACK；缺少 ROS 的跳过不计通过。
- RC 配置、链解析、控制器、命令解析、实例保存及客户端代次单测：**66 passed**，10.57 秒。该范围使用 example 或测试自带的轻量替身，不启动业务模型策略。
- Python 3.10 语法检查 5 个文件及 `git diff --check` 通过。未运行全量接口、Qwen/其他业务策略套件，也未启动 Docker、模型或硬件。

证据：`/private/tmp/rc-named-set-red.log`、`/private/tmp/rc-named-set-green.log`、`/private/tmp/rc-named-set-example-scope.log`、`/private/tmp/rc-named-set-rc-scope.log`。

## 注册热配置命令与 Qwen switch-task（2026-11-04）

两个 Qwen 注册 `switch-task <str>`，handler 返回 `params.task` 热补丁，RC 通过新增的协商命令 effect `update_config` 复用现有 SDK 热更新事务。example 的 `set-message` 展示相同公共契约。链内只修改明确目标实例，保留所有 context、进程和容器，目标及逻辑链版本递增；MCP 接管要求和控制代次复核保持有效。

- 最终 `python3 scripts/test_scope.py --interface`：**1479 passed、27 skipped**，531.66 秒，13 条既有 Pillow 弃用告警。跳过项不计通过。覆盖注册、能力协商、热白名单、原配置引用、链实例隔离、保存、MCP 权限及暂停后迟到结果/ACK 不提交。
- 自审发现原 `PolicyProcess` 热更新 ACK 未核验 `config_version`，新增缺失值、旧版本和字符串版本故障注入，先 3 项失败后修复；相关命令、Qwen worker、热更新与冷重载专项 **54 passed**。
- Humble / Python 3.10 新命令、ACK 校验及 example ROS 仿真：**22 passed**，12.60 秒。
- 真实 Docker 的两个 Qwen 通过 `switch-task` 更新任务并恢复默认任务，容器、host、runtime 和 PID 保持一致，测试后端 load/warmup 各一次，配置版本 1→3，RC 退出码 0。
- example / example 自链的 **7 组 Docker 检查通过**，实际 RC Python 3.10 → example Python 3.14。注册热命令更新第二个实例时，第一实例版本维持 4，目标从 4 到 5，两者容器、host、PID、模块和 context 不变；命令期间禁止调用 Docker。既有 lost-create 故障注入正确保留 RC 强制清理退出码 1，验收脚本退出 0。

证据：`/private/tmp/rc-switch-task-interface-final.log`、`/private/tmp/rc-switch-task-ack-green.log`、`/private/tmp/rc-switch-task-humble.log`，以及 `/private/tmp/rc-switch-task-docker/`、`/private/tmp/rc-switch-task-example-docker/` 的结果与清理 JSON。本轮单智能体实施和自审；测试部署及专用镜像标签已清理，保留其他任务使用的 Docker 引擎。未调用真实模型、供应商或机器人硬件。

## Qwen 任务热更新第一阶段（2026-11-04）

本节记录首次通过 `set policy.params.task` 接入热参数时的验证：两个 Qwen 复用既有白名单热补丁与配置版本确认，当时未改 RC/SDK 公共协议；后续注册命令的契约扩展见上一节。使用与旧配置迁移见 [Qwen 策略说明](qwen35-policy.md#任务文本热更新)。

- `python3 scripts/test_scope.py --policy qwen35_single qwen35_twostage`：**109 passed、9 skipped**，36.12 秒。包含 example 基线、两个正式入口和真实 RC/host/worker、热补丁版本同步、保存与 null 回退；宿主缺少模型依赖和 ROS 的跳过不计通过。
- Linux / Python 3.11.17 的 Qwen 配置、入口、调度、CPU 后端和 CUDA 回退套件：**67 passed、4 skipped**，11.99 秒。实际两阶段缓存测试确认任务变化后立即重新生成子任务、模型对象保持不变；4 项跳过均需要 NVIDIA CUDA 设备。
- 独立 Humble / Python 3.10 example ROS 仿真：**1 passed**，3.14 秒。
- 隔离 Docker 中分别运行两个 Qwen，RC Python 3.10 → policy Python 3.11。任务覆盖及 null 回退前后，容器 ID、host、runtime 和执行 PID 均保持一致，配置版本从 1 到 3，测试后端 load/warmup 各一次；更新期间拦截 Docker 调用，确认热更新路径不调用 Docker。RC 退出码 0，临时容器、卷和网络已清空。

CPU 容器首次运行遗漏正式 Compose 所需的 Transformers 补丁挂载，导致 `test_load_warmup_uses_checkpoint_config_and_rejects_wrong_chunk` 和 3 个 `test_fallback_temporaries_stay_on_the_input_device` 参数用例失败；补齐只读挂载后的上述最终套件全部通过，无需修改厂商源码。

证据：`/private/tmp/rc-qwen-task-scope.log`、`/private/tmp/rc-qwen-task-model-tests-final.log`、`/private/tmp/rc-qwen-task-ros.log`、`/private/tmp/rc-qwen-task-docker/{result.json,cleanup.json}`。本轮单智能体实施与自审；Docker 使用本机 OrbStack，模型推理由测试后端代替，未执行真实 GPU 模型或机器人硬件验收。

## 历史验收

本文保留 2026-10-27 的历史验收记录；源码外置后的复现命令已更新，最新验收见下方“源码挂载验收”。

日期：2026-10-27。代码与部署结构已重构为基础 RC/anchor 两服务、按目录发现的独立策略容器。本页区分自动化测试、真实容器、模型和硬件证据。

## 已执行的验证

| 验证 | 结果 | 范围 |
| --- | --- | --- |
| 完整主机套件 | **697 passed，27 skipped** | macOS/arm64，Python 3.11 测试环境；控制循环、配置、命令、MCP、SDK 进程、外部资源、TUI |
| 完整 Linux 套件 | **672 passed，27 skipped** | ROS 2 Humble / Python 3.10.12 容器，包含实际 ROS 仿真闭环；只读挂载当前源码 |
| 实际动态 Docker 验收 | **10 组通过** | 基础拓扑、IPC、Py3.10→3.14、冷切换、故障与恢复，见下表 |
| 公共宿主 down | **通过，返回 1** | 已确认强制清理的 RC 非零退出码被保留，策略与基础资源完成收尾 |
| 镜像构建 | **RC、hold、anchor、测试镜像成功** | SDK/RC 0.9；RC 内 Docker CLI 28.5.1、Compose 2.39.1 |
| 正式 RC 镜像源码审计 | **12 个关键模块逐字节一致** | controller、runtime manager/spec、external、host client/process/chain，以及 SDK catalog/host/lifecycle/supervisor/commands |
| 本机配置值审计 | **8 份保留** | 除 schema、移除 host/deployment 表和 Qwen 相对模型路径的预定转换外，既有值不变；备份保留在 .setup/backups |
| 构建声明与插件发现 | **通过** | 无 example 的未知策略、发现不 import、两 Qwen 独立完整声明与相同有效输入去重、Dockerfile 专属 ignore 差异检查 |

两套完整测试各有 13 条既有 Pillow/Textual-image 弃用警告。跳过项依赖特定平台、可选模型依赖或外部服务；没有将跳过项记作通过。

真实容器环境为本机 OrbStack 的 Linux/arm64 daemon：Engine 29.8.2，宿主 Compose 5.5.0。它的 socket GID 在容器内会映射，因此验收夹具探测了实际映射 GID。正式部署目标仍为本地 Linux Docker；这份证据不代替 Linux/amd64 目标机、rootless、远端 daemon 或其他 Desktop 平台验收。

## 实际容器检查

`tests/dynamic_docker_smoke.py` 为每轮创建唯一临时部署，使用含空格的项目路径及项目外 bind mount。容器内脚本 `tests/container_dynamic_smoke.py` 使用真实 RC、SDK host、Docker CLI 与 mock 机器人。

1. startup_policy=null 时只有两个基础容器，active_policy 为空；静态帮助不会创建策略。
2. RC Python 3.10 调用 Python 3.14 策略；策略实际映射只读图像，独立外部目录挂载可读。
3. 同 ID 冷切换产生新 CID 与 host instance；默认 switch 不复位。
4. `[a,b,a]` 只创建两份实例；切 `[b,c]` 的事件记录证明全部旧 CID 删除后才执行新 create；同镜像仍有独立实例，歧义裸命令被拒绝。
5. 删除旧 Compose 文件后仍可按 CID/标签清理。
6. 单策略 initialize 失败后，活动身份清空且无残留容器。
7. 链部分初始化失败时回收本次全部成员。
8. release 卡死时在预算内强制清理，后继保持暂停，强制/非零事实保留；显式 --reset 由控制线程执行运动。
9. 新 RC run 回收旧 run 的持久创建意图/容器，并在同 anchor 中实际清除旧 POSIX 图像池段。
10. anchor 更换/出现多实例时清理旧策略并阻止新组启动。另有故障注入覆盖同 CID 重启造成的 IPC 代次变化。

公共 `down` 额外验证：读取已停止 RC 的 ExitCode，在精确资源证明齐备后移除基础设施，仍返回 1；没有把 Docker stop 命令成功误认为 RC 正常退出。

独立代码审查发现的退出期限、退出码、共享内存恢复、有效 build ignore、业务 JSON 误识别问题均已补失败回归后修复。额外回归覆盖启动恢复失败时禁止手动 switch、daemon 身份变化、无法确认的迟到 create、错误标签、外部 instance 变化和目录别名。

## 复现

主机测试需要合适的开发依赖及本地 UDS/POSIX 共享内存权限：

```bash
python3 -m pytest -q
```

Linux 验证镜像：

```bash
docker build -f docker/rc.Dockerfile --target test -t robot-control-compose-dynamic-tests:local .
docker run --rm --shm-size=512m \
  --user "$(id -u):$(id -g)" --workdir /workspace \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  -e HOME=/tmp \
  -e RC_SOURCE_MODULES='{"robot_control":"/workspace/src","rc_policy_sdk":"/workspace/packages/policy-sdk/src"}' \
  -e PYTHONPATH=/workspace/src:/workspace/packages/policy-sdk/src:/workspace/packages/qwen35-policy/src:/workspace/packages/llm-direction-policy/src:/workspace/packages/llm-failure-policy/src:/workspace/packages/llm-provider-config/src:/workspace/packages/pi05-policy/src:/workspace/ros_ws/src/robot_control_test_sim \
  -e PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  robot-control-compose-dynamic-tests:local \
  python3 -m pytest -p pytest_asyncio.plugin -q -p no:cacheprovider
```

动态容器验收需先构建上述测试镜像、hold 与 anchor：

```bash
docker build -f policy/hold/Dockerfile -t robot-control-compose-policy:local .
docker build -f docker/ipc-anchor.Dockerfile -t robot-control-compose-anchor:local .
python3 tests/dynamic_docker_smoke.py
```

脚本仅操作带本轮唯一部署/project 标签的资源，结束后清理其容器、网络和卷。生产启动与收尾使用 `scripts/compose.sh`，不复用验收部署身份。

本轮完整日志、事件时间线和源码哈希保存在执行机 `/private/tmp/rc-dynamic-runtime-20261027/`。它们是运行证据，不提交仓库。

## 未执行的验收

未连接实体机器人、未启动厂商硬件驱动、未调用真实付费供应商、未进行真实 Qwen/GPU 推理。本轮没有构建或验证新的 Qwen CUDA 镜像运行实例；Qwen 本机子进程测试使用轻量 backend。实际权重、CUDA/NVIDIA runtime、模型延迟、真实相机和动作保持/显式复位仍需在目标机器按现场安全流程验收。

## 源码挂载验收

`tests/source_mount_docker_smoke.py` 创建独立临时部署，检查新依赖镜像在无源码挂载时无法导入项目包，然后修改临时实现包、冷切换 policy、完整退出并重建 RC/anchor。它核对源码行为变化而镜像 ID 不变，包含原子替换编辑、故意保留的旧宿主字节码、只读挂载、缺失源码拒绝和失败后的清理。

可通过 `RC_TEST_RC_IMAGE`、`RC_TEST_ANCHOR_IMAGE`、`RC_TEST_WORKER314_IMAGE` 指定本次新建的依赖镜像；全局字典夹具另接收 `RC_TEST_WORKER311_IMAGE`。这些变量只选择验收镜像，不修改本机部署配置。测试 RC 镜像应选 `docker/rc.Dockerfile --target test`。默认动态镜像名称与上方构建命令一致。

```bash
docker build -f policy/llm_failure/Dockerfile --target runtime -t robot-control-compose-llm-failure:local .
export RC_TEST_RC_IMAGE=robot-control-compose-dynamic-tests:local
export RC_TEST_ANCHOR_IMAGE=robot-control-compose-anchor:local
export RC_TEST_WORKER314_IMAGE=robot-control-compose-policy:local
export RC_TEST_WORKER311_IMAGE=robot-control-compose-llm-failure:local
python3 tests/source_mount_docker_smoke.py
python3 tests/global_state_docker_smoke.py
python3 tests/dynamic_docker_smoke.py
```

Qwen 导入探针 `tests/qwen_image_probe.py` 必须使用与策略 Compose 相同的 SDK、实现包、vendor 和直接补丁挂载；它核对第三方版本、源码位置和当前源文件哈希，不要求本项目包的已安装发行元数据。Pi05 的 PYTHONPATH 不包含 Qwen vendor，使用镜像中锁定的官方 LeRobot。GPU 和真机测试仍需在目标设备另行执行。

## 链按环节独立实例验收（2026-10-30）

`[a,b,a]` 现展开为 `a-0,b,a-1`，每个环节独占容器、host、执行进程和 context；本节替代历史验收中的“重复成员复用”行为描述。

- 完整主机套件：1003 passed，29 skipped；Python 3.11。另有 globals 超时断言跨版本兼容检查 5 passed。
- 完整 Linux/ROS 套件：940 passed，31 skipped；ROS 2 Humble / Python 3.10。两套完整测试各有 13 条已有 Pillow 弃用警告。
- 动态 Docker：10 组通过，覆盖三个独立容器/host/socket/网络、原源码路径与同镜像、独立 command/monitor、整组退出屏障、部分启动失败、恢复及强制清理。强制清理场景的公共 down 按预期返回 1，验收脚本返回 0。
- 完成结果 Docker：4 组通过，覆盖实例来源、MCP 等待/取消和后续环节故障。
- globals Docker：4 组通过，实际运行 Python 3.10 RC、Python 3.14 的两个独立 a 实例及 Python 3.11 b；共享状态、独立计数、重启及 SIGTERM 持久化正常。
- 独立审查发现的重新应用链读取旧来源缓存问题已补失败回归并修复。真实 Docker 暴露的 Compose 默认网络重名问题已修复，自有网络名称随实例项目隔离。

本轮证据目录为 `/private/tmp/rc-chain-instances-20261030/`：`host-final.log`、`linux-verified.log`、`dynamic-final/container-smoke.json`、`result/cross-container.json`、`global-final/cross-container.json`。验收仅使用隔离测试部署，结束后清理所属容器、网络和卷；未进行真实机器人、GPU 模型或外部供应商验收。

## 连续录制验收（2026-10-30）

`record` 增加本地异步启停、独立 30 Hz 三路 PNG 与四份 CSV、完整状态／控制／逐实例推理日志、配置历史及 LLM 请求诊断。格式、容量和复现命令见 [连续录制文档](recording.md)。

- 最终完整主机套件：**1107 passed，29 skipped**，Python 3.11，366.58 秒。
- 最终完整 Linux/ROS 套件：**1032 passed，31 skipped**，ROS 2 Humble / Python 3.10，494.54 秒。两套各有 13 条既有 Pillow 弃用警告。
- 录制专项覆盖真实 SIGSTOP、真实 EFBIG、字节／条数满、SDK 日志洪泛、反复启停、跨会话缺口归属、绝对退出期限和 SIGTERM。采样额度保留到 writer ACK；强制退出后核对进程、管道和线程清理。无发送、混合模式、逐臂部分失败、MCP 控制权和推理等待均有回归。
- 独立审查发现并修复两项问题：图像池所有槽都被租约占用时，记录实际推理 lease 对应帧；普通日志条数达到上限时，仍给关键回执和 LLM 事件保留额度。另补齐最终退出摘要和诊断缺口的会话归属。

最终性能验收在其他 CPU 测试暂停后顺序运行，源码哈希与最终生产代码一致：

| 指标 | 动态 Mock，三路 640×480 | 真实 ROS，三路 64×48 |
| --- | ---: | ---: |
| 时长／采样数 | 60.002473 秒／1801 | 10.007046 秒／301 |
| 每路有效写入频率 | 30.015430 Hz | 30.078807 Hz |
| 采样截止缺口／缺失帧／附加丢弃 | 0／0／0 | 0／0／0 |
| 在途信用峰值／观察到的采样队列峰值 | 149797208 bytes／8 | 2590160 bytes／7 |
| 控制发送调用耗时 P99／最大 | 0.591678／3.159562 ms | 2.724691／15.702186 ms |
| 含尝试记录的控制调用耗时 P99／最大 | 1.605949／5.437733 ms | 3.900286／15.935230 ms |
| 实际逐臂发布／HTTP 和 SDK 请求对 | 5998／59 | 1000／10 |

Mock 核对 5394 个唯一采样 PNG 的无损像素、四份 CSV 和实际 policy 输入图像。每路重复帧和来源序号跳跃均为 3，均保留在采样元数据中。ROS 的 1000 次 ACOne 成功发布与 simulator 收到的模式和数值完全一致；本节调用耗时不代表真实机器人响应延迟。两轮停止后待写入归零，无强制清理或残留容器。

证据位于 `/private/tmp/rc-record-20261030/`：`host-verified.log`、`linux-verified.log`、`manual-mock60-isolated/report.json`、`manual-ros10-isolated/report.json` 及各自的 `teardown.json`。另保留主机套件并发时出现 1 个采样截止缺口的失败压力报告 `manual-mock60-frozen/report.json`，没有改低门槛记为成功。

首次全套暴露的旧时间夹具、监控后台发送时序及新增日志缺口断言已更新。一次 Linux TUI 退出非零未在隔离 4 项测试及最终全套中复现；测试现在保留末尾 RC 事件用于诊断，最终运行日志含 `rc.exit`、`writer_drained=true`、`exit_code=0`，没有为此修改退出控制逻辑。

验证没有增加 SDK 依赖、Compose 服务、厂商驱动或模型请求重试。此轮 Mock、ROS 仿真和本地 HTTP 验收不替代真实相机、机器人或外部模型供应商验收。

## llm_failure GPT Decisions 后端（2026-10-30）

新增默认关闭的 `params.decision_api` 配置组，启用后使用独立模型、地址、密钥和超时；关闭或省略时保持 DeepSeek。请求为单个二选一 choice 问题，严格解析为失败布尔值，不重试或回退。详见 [配置与协议说明](llm-failure-policy.md)。

- 锁定 OpenAI SDK 3.37.0 的完整主机套件：1131 passed，29 skipped，363.78 秒。
- 完整 Linux/ROS 套件：1032 passed，31 skipped，412.74 秒。两套均有 13 条已有 Pillow 弃用警告。
- Python 3.11 / OpenAI 3.37.0 策略镜像：95 passed，24.86 秒；只读补入已锁定的纯 Python YAML 依赖，不改变模型镜像依赖。RC/worker 集成在主机套件中验证。
- 覆盖实际本地 HTTP 请求、非法选项、拒答、超时、选中配置的期限、两组密钥脱敏、token 用量、录制日志、reset 后迟到结果和请求期间图像租约释放。独立代码审查未发现待修问题。

首次 Linux 全套触发既有 MCP 测试的启动竞态：HTTP 服务已监听，但 policy 仍在 switch，提前 take 正常返回 `busy: switch`。通过延迟测试 worker 启动 0.2 秒稳定复现，仅修正测试为等待初始化完成；生产控制逻辑不变。相同延迟实验转绿，MCP 模块 7 项通过，随后完整 Linux 套件通过。

证据目录：`/private/tmp/rc-failure-decisions-20261030/`，包括 `host-full.log`、`linux-verified.log`、`policy-linux-verified.log`、`incremental-sdk337.txt` 和 `mcp-delayed-start-red.txt` / `mcp-delayed-start-green.txt`。未调用真实收费 API，也未进行真机准确率验收。

## WebUI 直接命令与文本批次（2026-11-04）

新增 `llm=false` 直接模式和人工 MCP `tui_command/cancel_tui_command`。文本批次无需 take、FIFO 逐行执行；本地 TUI 的非空输入清除未开始的行和等待批次，当前已执行行沿用原生命周期。实现及使用见 [WebUI](webui.md#直接命令模式) 和 [MCP](mcp.md#人工文本批次)。

按用户指定范围仅做定向验证，未运行 `test_scope.py`、`--interface`、整个 tests 目录或业务 policy 套件：

- 新增批次、MCP、两个真实 example worker 场景，以及直接受影响的 TUI/控制代次/日志节点：**43 passed**，6.75 秒，`/private/tmp/rc-tui-targeted-backend-final.log`。
- 六个相关前端测试文件：**43 passed**，`/private/tmp/rc-tui-web-unit-final.log`；类型检查和生产构建通过，`/private/tmp/rc-tui-web-build-final.log`。
- 固定 Nginx/njs 的真实容器配置测试：**10 passed**，2.77 秒，`/private/tmp/rc-tui-nginx-green.log`。初次发现 njs 不支持对象展开，改用先验证字段再 Object.assign；依赖未新增。
- Chromium 开发模式：四个直接模式加一个 LLM 兼容用例 **5 passed**，14.4 秒；新增提交前停止竞态 **1 passed**，4.4 秒。日志为 `/private/tmp/rc-tui-dev-browser.log` 和 `/private/tmp/rc-tui-dev-stop-before-submit.log`。
- 生产 Nginx：LLM 同源代理兼容 **1 passed**，3.0 秒；读取真实 `{"llm":false}` 私有配置的五个直接模式用例 **5 passed**，11.8 秒。证明无需 API 配置、AI 路由返回 503、文本原文仅提交一次、无需 take、递归整批拒绝、停止保留当前行收尾、刷新不取消或重发。日志为 `/private/tmp/rc-tui-production-llm.log` 和 `/private/tmp/rc-tui-production-direct-verified.log`。
- 同一隔离生产部署运行 mock RC 和动态 example 容器，8 组 Compose/HTTP 检查通过；状态、直接配置、统一清理证据在 `/private/tmp/rc-tui-production/`。截图在 `/private/tmp/rc-tui-production-direct-verified/`，已核对三路实时 mock 图像、命令模式和批次成功回执。

额外修复经失败用例验证的边界：生命周期丢弃子命令后清除来源记录，已终态队列请求不再执行；TUI 清队列不撤销已经开始的 exit；敏感的非法本地输入仍清队列且不泄露；停止请求立即发出，不等待下一次回执轮询。

环境记录：旧 RC 镜像缺少已锁定的 ruamel.yaml；直接构建请求 Docker Hub 元数据时返回 401，改为继承本地 RC 镜像并安装仓库完整 rc.lock，通过 pip check。随机宿主端口在测试容器重启后变化，重新发现端口后生产用例通过。失败启动和旧端口用例不计通过。没有真实模型、ROS 仿真或硬件验收，没有修改用户私有配置。

## WebUI 同源 AI 代理（2026-11-04）

真实 endpoint 与 Key 由 WebUI 服务端私有 JSON 提供，Nginx 代理固定 `/ai/chat/completions`；浏览器只读取 `enabled/model/timeout_s`。复用基础镜像自带 njs 生成启动配置，未增加生产 Node/Python 后端。迁移步骤见 [WebUI 配置](webui.md#api-配置)。

- 前端、共享配置与真实 HTTP 开发代理：**35 passed**，类型检查和生产构建通过。覆盖无 CORS、服务端鉴权、固定目标、超时、重定向和错误内容脱敏。
- 实际只读 Nginx/njs 镜像配置测试：**9 passed**，2.28 秒；检查非 root、0600 私有配置、公共 JSON、字面 URL、非法 JSON/配置不输出凭据。
- Chromium 开发模式：**9 passed**，23.1 秒；最终生产 Nginx 模式：**9 passed**，29.9 秒。实测上游 OPTIONS 返回 403 且没有 CORS 许可，页面仍能经同源代理完成查询/录制/复位/策略切换。浏览器请求无 AI Authorization，公共配置无 endpoint/key；MCP、三路预览、移动布局和错误时不执行操作均通过。
- 生产 Compose 另验证私有文件不在网页目录、独立 AI/MCP 鉴权、POST/JSON/Origin 约束、客户端 query 不能改写上游、字面 `$host` 不被展开、401 与 418 保留状态并隐藏错误内容。上游 URL 的字面 `$` 按 `%24` 发送；Nginx 内部 client-abort 499 不能配置为 error_page，配置生成显式排除它。
- 项目默认 example 范围：**60 passed、1 skipped**，46.27 秒；宿主缺少 ROS 的跳过不计通过。本轮没有修改 RC/SDK 契约或调用真实模型、机器人硬件；并行 Qwen 工作的验证另见其记录。

日志：`/private/tmp/rc-webui-ai-proxy-unit-final2.log`、`/private/tmp/rc-webui-ai-proxy-build-final2.log`、`/private/tmp/rc-webui-ai-proxy-nginx-final.log`、`/private/tmp/rc-webui-ai-proxy-dev-e2e-final.log`、`/private/tmp/rc-webui-ai-proxy-e2e-verified.log`、`/private/tmp/rc-webui-ai-proxy-example-scope.log`。隔离 Docker 与清理证据在 `/private/tmp/rc-webui-ai-proxy-docker-verified/`，标准镜像复核在 `/private/tmp/rc-webui-ai-proxy-delivery/`；浏览器截图单独保存在临时目录。实测环境为 macOS/OrbStack Linux arm64、Chromium，Browser 插件不可用，使用已有 Playwright。

以下首版记录保留当时浏览器直连 API 的历史实现和性能证据；当前部署使用上述同源代理。

## Vue 三路 WebUI 首版（2026-11-04）

采用 Vue 静态前端、独立 Nginx 容器和现有 RC HTTP/MCP。三路监控独立读取相机缓存，不使用 policy 共享图像租约。运行与配置说明见 [WebUI](webui.md)。

- 最终 RC 相关回归：58 passed，41.63 秒，覆盖预览、相机缓存、MCP、控制代次、CLI 和统一清理期限。
- 项目默认 `scripts/test_scope.py`：51 passed、1 skipped，30.54 秒；宿主缺少 ROS 的跳过不计通过。另在 Humble 容器运行 example ROS 仿真：1 passed，2.71 秒。
- Humble / Python 3.10 的最终预览与控制代次专项：10 passed，4.88 秒。
- 前端单测：12 passed；类型检查、Vite 生产构建通过。最终 Nginx 镜像的 Chromium 浏览器流程：7 passed，23.3 秒，覆盖路由刷新、窄屏底部输入框、复位、整份翻译校验、CORS/超时、录制例外、人工干预、策略自链切换与继续、过期状态及关闭后的订阅释放。
- 隔离 Docker 使用生产 Compose overlay、真实动态 example/self-chain 容器和本地兼容 API 替身。检查只读运行、无 Node 运行时、无共享内存/RPC/Docker socket 挂载、同源代理和 token 转发；非本地 Host 被代理拒绝。

本机 macOS / OrbStack Linux 容器，Chromium 156.0.8078.4 headless 活动页面，1440×900；三路带像素序号的 mock 源均为 640×480、30 Hz。example 自链保持 running，连续测量 600.009 秒。浏览器按 JPEG 中的源序号统计真正更新的图像，不把 RAF 或重复旧帧计为新帧。

| 相机 | 源帧率 | 编码帧率 | 输出帧率 | 浏览器实际新帧率 | 延迟 P50 / P95 / 最大值 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 左腕 | 30.00 | 29.93 | 29.92 | 29.89 fps | 50.8 / 58.8 / 473.8 ms |
| 头部 | 30.00 | 29.93 | 29.92 | 29.87 fps | 51.8 / 73.8 / 318.5 ms |
| 右腕 | 30.00 | 29.92 | 29.91 | 29.88 fps | 52.2 / 57.2 / 510.8 ms |

三路 10 分钟均值均超过 29.5 fps 门槛，浏览器脚本错误为 0。存在短时抖动：最低 10 秒窗口约 28.65 fps；末尾完整窗口恢复约 30.05 fps，未出现旧帧持续积压。延迟由 mock 源帧时间与浏览器取样时间计算，经一次跨进程时钟偏移校正，包含显示采样误差。

59 次资源采样中，RC Docker CPU 均值 30.26%，Nginx 1.52%（Docker stats 口径）；RC 内存范围 259.1–272.1 MiB、首尾 263.3→267.7 MiB，Nginx 8.47–9.64 MiB、首尾 8.93→9.07 MiB。浏览器渲染进程 CPU 均值约 35.7%，JS heap 4.15–5.69 MiB，后者包含测量脚本保存的延迟样本，不代表完整浏览器 RSS。资源有正常波动，未观察到随帧数持续积累的发送队列。慢读者、无订阅时停止编码、迟到编码结果和期限耗尽另有专项回归。

证据：`/private/tmp/rc-webui-benchmark-600/result.json`、`/private/tmp/rc-webui-docker-verified/`；最终页面截图在 `/private/tmp/rc-webui-delivery/`。最终日志为 `/private/tmp/rc-webui-rc-final.log`、`/private/tmp/rc-webui-scope-final.log`、`/private/tmp/rc-webui-humble-final.log`、`/private/tmp/rc-webui-example-ros.log`、`/private/tmp/rc-webui-unit-final.log`、`/private/tmp/rc-webui-docker-e2e-delivery.log`。

10 分钟测量后只修正窄窗口固定输入框和预览退出期限，编码/发送路径保持相同；最终镜像重新通过上述浏览器流程。真实相机、机器人硬件、目标真机负载及真实 OpenAI 兼容供应商尚未验收；本地 mock 与 ROS 仿真不替代这些验收。

## LLM 供应商文件与实例切换（2026-11-02）

主机完整套件（Python 3.11）1086 passed、17 skipped；ROS 2 Humble 完整套件（Python 3.10）997 passed、19 skipped。两个 LLM Python 3.11 运行镜像均从最终锁文件构建，并在禁网容器内通过实际 Compose 源码挂载初始化，覆盖方向策略四份、失败检测两份 YAML 配置。

本地 HTTP 验证了切换后第一次请求的接口、模型及凭证。重复实例链覆盖校验失败保留旧组、同路径重读、成功后暂停、显式保存到来源目录。跨容器失败检测使用 mock 机器人，4 次本地 HTTP 请求后正常退出，退出码为 0。

两套测试各有 13 条 Pillow 弃用告警。Linux 的两条录制器强制退出用例另有线程告警；在起始提交 `689822c` 和本次代码上单独复测，两项均通过且未重现告警。


## 2026-11-05 WebUI 连接宿主机 RC

生产 WebUI 新增 `RC_WEBUI_UPSTREAM`，默认容器服务名保持动态 DNS；真机目标从 `/etc/hosts` 解析 host-gateway IPv4。未修改 RC/SDK 控制语义。WebUI 67 项测试与构建通过，实际固定 njs/Nginx 配置和 Docker 网络用例 17 项通过；旧镜像在宿主机连接用例中返回 502，新镜像在默认 bridge 和 host 上游下均通过状态、MCP 请求、三路 multipart 及认证头转发验证。默认与真机 Compose 合并检查通过。

Python 3.11 example 基线 78 通过、1 项因缺少 ROS 环境跳过（不计通过）；example Docker 验收 7 项通过，容器和卷清理确认。故意注入的丢失 create 回执触发强制清理并保留 RC exit 1，属于该用例的预期断言，验收脚本退出 0。Docker 证据来自 OrbStack 的 Linux ARM64 引擎，未运行真机、厂商驱动、真实供应商或图像帧率验收。初次本地端口权限失败和 njs 默认参数语法失败均已修正执行方式或代码后重跑，未计为通过。

## 2026-11-05 WebUI AI 上游多级证书链校验深度

真机部署机（10.6.3.69）WebUI 发送自然语言消息返回 502，而 `/health`、`/preview/status` 与三路图像正常，说明 RC 代理链路可用、故障只在 `/ai/chat/completions`。Nginx 日志为 `upstream SSL certificate verify error: (20:unable to get local issuer certificate)`，上游 `api.tensoris.ai`。宿主 curl 与容器内 wget 均可完成 TLS 握手，容器信任库包含 ISRG Root X1/X2，因此不是 CA 缺失；`openssl s_client -verify_depth 1` 在该链（leaf → YE2 → Root YE → ISRG Root X2）上复现 `certificate chain too long` 与 `unable to get local issuer certificate`，`-verify_depth 2` 验证通过，确认为 Nginx `proxy_ssl_verify_depth` 默认 1 级对 Let's Encrypt 新层级过浅。

生成的 AI 代理配置增加 `proxy_ssl_verify_depth 4` 并补充单测断言；未修改 RC/SDK 控制语义，监控与 MCP 转发路径不变。WebUI 67 项单测与前端构建通过（2026-11-05）。部署机需重建 WebUI 镜像后复测自然语言发送；本轮只读检查没有在部署机启动、重建或修改任何容器，现场复测结果未计入通过证据。

## WebUI 流式 AI、玻璃交互与暂停／恢复（2026-11-05）

同源 AI 请求改为 SSE 流式接收，支持真实思考字段与指令说明预览；仅完整 `[DONE]` 和有效白名单 JSON 可以交给执行器。开发代理逐段转发，生产 Nginx 关闭缓冲／压缩并保留固定上游、私有鉴权、TLS 和错误脱敏。悬浮历史保存本页最近 50 次请求，取消、迟到响应、确认结果和各阶段光效分开处理。顶部新增直接执行的暂停／恢复按钮，在两种 LLM 模式中均可用。部署只需重建 WebUI，配置字段及 RC/SDK/policy 契约不变。

- 全部 WebUI 单测 **99 passed**，最终类型检查及生产构建通过。覆盖 Unicode／转义分片、思考别名、选择隔离、未结束／截断／拒答、长度限制、超时取消、代理首段和客户端断开，以及历史、回执和手动暂停优先级。日志 `/private/tmp/rc-webui-stream-unit-complete.log`、`/private/tmp/rc-webui-stream-build-complete.log`。
- Chromium 开发模式完整浏览器套件 **20 passed，48.2 秒**；真实生产 Nginx + mock RC + 动态 example 容器完整套件 **20 passed，约 1.1 分钟**。覆盖 example 自链、原直接命令批次、新流式和恢复语义、两种模式的暂停／恢复及移动布局。日志 `/private/tmp/rc-webui-stream-toolbar-browser.log`、`/private/tmp/rc-webui-stream-complete-browser.log`。
- 固定镜像 njs/Nginx 配置和真实网络检查 **18 passed，6.43 秒**。新增生产首段测试通过独立 gate 保证：客户端读到首段后，上游才获准发送 `[DONE]`。日志 `/private/tmp/rc-webui-stream-nginx.log`。
- 项目默认 `python3 scripts/test_scope.py`（现有 Python 3.11 测试环境）**78 passed、1 skipped，58.47 秒**；跳过原因为宿主缺少 ROS/rclpy，不计通过。日志 `/private/tmp/rc-webui-stream-example-scope.log`。未启动其他业务策略或真实硬件，未调用真实模型供应商。
- 已检查桌面 1440×900、手机 320／390 和平板 768 宽度截图；末轮提高辅助文字对比度后，再完成 **4 项定向浏览器验收**及构建。截图 `/private/tmp/rc-webui-stream-visual-final/`，日志 `/private/tmp/rc-webui-stream-visual-final.log`。系统减少动态效果时旋转停止，直接模式无 AI 历史区。

检查中修正了：手动查询的迟到回执覆盖新请求光效、待确认时只读查询的历史归属、窗口尺寸变化误判为手动回看、展开回执后新请求输出被隐藏。生产浏览器首轮存在一个测试时序问题：点击查询后立即读取了旧文案，而页面快照已确认回执；改为等待异步渲染结果，最终完整套件通过。主动取消按本次设计使用独立取消状态，旧“必须为红色错误”断言相应更新。

验证使用现有 Playwright；当前会话没有 Browser 插件，匹配浏览器下载在临时目录。首次环境运行缺少子进程 PYTHONPATH 和浏览器，均作为失败记录，修正后重新验收。自审确认没有修改控制／租约契约，没有自动重试写操作，没有把思考或半份指令当作执行结果。

两个隔离生产部署均以清理退出码 0 结束，容器、policy 实例和卷全部清空；临时 WebUI 镜像已移除，Docker 恢复原先停止状态。清理证据 `/private/tmp/rc-webui-stream-cleanup-final.json`，未修改用户私有配置或替换原有标准镜像。

## WebUI 动态 MCP 助手与 effort（2026-11-05）

在流式界面和暂停／恢复基础上，改为每次刷新能力目录、按真实结果逐轮调用一个工具的助手。增加近期对话、独立六部分提示词、原生参数校验、两类回执和可选 effort；七个 MCP 写工具补齐可选控制代次，终态操作记录不可变完成代次。此次需要同步更新 RC 与 WebUI，SDK/policy 公开契约不变。

- WebUI 完整单测 **126 passed**，类型检查和生产构建通过。日志 `/private/tmp/rc-webui-agent-unit-verified.log`、`/private/tmp/rc-webui-agent-build-verified.log`。覆盖动态描述、类型／实例校验、上限、结果节选、上下文、effort、流式异常、取消和未知受理，不调用真实模型。
- 开发代理完整 Chromium 套件 **27 passed，约 1.1 分钟**；生产 Nginx、mock RC、真实动态 example 容器完整套件 **27 passed，约 1.6 分钟**。日志 `/private/tmp/rc-webui-agent-dev-final.log`、`/private/tmp/rc-webui-agent-production-final-browser.log`。验证配置查询→接管→policy 命令→查询核实、链应用后使用新实例、completed/terminated、刷新恢复任务回执、无图像运动与反馈、异常响应不重发，以及既有流式、直接批次、监控和工具栏。
- 最后收紧目录完整性检查后，开发与生产分别补跑 **7 passed**（21.5 秒、33.5 秒）。初次误把 SDK 明确允许的 `timeout_s=-1` 当作非法，真实浏览器验收暴露问题；修正为接受 -1、拒绝缺失字段，并增加回归。最终日志 `/private/tmp/rc-webui-agent-description-verified-dev.log`、`/private/tmp/rc-webui-agent-description-verified-production.log`。
- 真实固定 njs/Nginx 的配置、effort、网络、流式首段、固定上游与错误脱敏套件 **29 passed，9.35 秒**。日志 `/private/tmp/rc-webui-agent-nginx-delivery.log`。新增 effort 用例首次缺少旧配置必填项，补全测试配置后通过；失败不计通过。
- 受影响 MCP、控制代次、运动、策略命令／重载、业务结果、直接批次和日志专项 **145 passed，70.90 秒**。日志 `/private/tmp/rc-webui-agent-backend-expanded.log`。默认 `python3 scripts/test_scope.py` 使用 example 和 example 自链：**78 passed、1 skipped，56.31 秒**；宿主缺少 ROS/rclpy 的跳过不计通过，日志 `/private/tmp/rc-webui-agent-scope.log`。

自审修正了两个结果边界：手动查询到 terminated 时保持中性、不闪绿；缺少结构化内容的写响应按受理未知保留锁定，不当作拒绝并自动解锁。原始回执、失败原因和迟到响应保护仍由同一执行器负责。已检查桌面与手机截图，产物保存在 `/private/tmp/rc-webui-agent-production-final-browser/`。

验收使用本地 API 替身、隔离测试配置和 mock 机器人，没有访问真实模型、厂商驱动或硬件，没有改用户私有凭据。临时部署清理证据位于 `/private/tmp/rc-webui-agent-production/cleanup.json` 与 `/private/tmp/rc-webui-agent-production-final/cleanup.json`；没有提交任务账本或运行产物。

两个临时项目均清理退出 0，容器、policy 实例与卷为空。测试镜像已移除，Docker 恢复初始停止状态，汇总 `/private/tmp/rc-webui-agent-cleanup-final.json`。改动保留在原工作区，未提交或推送。

## WebUI policy 等待选择与人工工具栏修正（2026-11-05）

提示词默认使用普通 `return(wait=false)`，只有当前实际运行的 policy／链成员明确会回传本次任务结果且用户需要该结果时才选择 `return_and_wait`。明确区分归还回执与业务终态，说明 `timeout_s=0` 仍创建业务等待任务。暂停／恢复／复位按钮改为共用执行器的单行 `tui_command`，不自动 take；按运行状态选择暂停或恢复，保留原提交代次、忙碌与待确认保护。

沿用用户“不重新测试”的要求，本轮未运行单测、浏览器或后端测试，也未启动 Docker 或连接模型／硬件。已更新提示词、普通归还回执、两种控制归属／LLM 模式的三个按钮及迟到模型响应的回归用例，尚未执行，不能计为通过。`npm run build`（含 `vue-tsc --noEmit`）通过，日志 `/private/tmp/rc-webui-human-toolbar-build.log`；`git diff --check` 通过。RC、SDK 和 policy 实现未改变。

## WebUI 日志、单图与浏览器视频导出（2026-11-06）

新增本页完整 AI 日志 JSON 下载、三个监控子路由、每路固定 30 fps VP8/WebM 下载及日志折叠按钮。图像接收和编码在浏览器 Worker 内执行，数据按页面会话分块存入 IndexedDB；RC、SDK、policy 公开契约和控制行为不变，部署仅需更新 WebUI。

- WebUI 全部单测 **157 passed**，类型检查及生产构建通过。覆盖完整日志不受展示条数限制、原始模型片段和无效尝试、HTTP错误、迟到响应归属、取消前保持连接、原请求的回执恢复关联、MJPEG任意分片、30fps时间轴、后台缺帧与网络突发、导出文件分块和头部回填。日志 `/tmp/rc-webui-reviewed-unit.log`、`/tmp/rc-webui-delivery-build.log`。
- 完整 Chromium 浏览器回归 **35 passed**，覆盖实际视频播放／拖动／重复导出、缺帧时保持旧画面、挂起恢复、单图路由、缓存隔离、存储耗尽后的部分导出、不支持编码时继续监控、执行中的日志快照和折叠状态，以及原有 AI／直接命令／控制回执流程。初轮已有一次在源码热更新时导航重置造成的失败，固定代码后完整重跑通过；失败不计通过。桌面与320／390／768宽度截图已检查，最后日志关联修复后再次完整回归 **35 passed，1.7分钟**；交付截图与视频证据 `/tmp/rc-webui-delivery-browser`，日志 `/tmp/rc-webui-delivery-e2e.log`。
- `.venv/bin/python scripts/test_scope.py` 默认 example 和 example 自链回归：**78 passed、1 skipped，64.54秒**。跳过项需要 ROS 2/rclpy 及消息包，不计通过。日志 `/tmp/rc-webui-example-scope.log`。
- 固定生产构建、同机单前台 Chromium156.0.8078.4、三路30Hz mock源，连续 **600.0092秒**同时显示和录像：左腕 **29.9029fps**、头部 **29.8979fps**、右腕 **29.9029fps**，均超过29.5fps要求；显示延迟P95为24.3～26.0ms，浏览器异常0。采样最大编码／存储积压6帧；主页面JS堆8.05～11.90MB，临时浏览器存储最终约229.04MB。JS堆指标不代表浏览器全部进程内存。结果 `/tmp/rc-webui-recording-bench/results/result.json`。
- 三个实际视频经ffprobe读取全部帧：左腕18007帧／600.233秒、头部18241帧／608.033秒、右腕18473帧／615.766秒；均为VP8、640×480、30/1fps，时长与帧数误差小于2ms。文件按各自点击时间截止，因此依次下载的视频时长递增。视频和截图位于上述结果目录。

验收使用本机隔离mock RC、example worker和API替身，没有连接真实模型、厂商驱动或机器人。本轮未执行Docker/Nginx容器或真机验收；静态构建验收不替代这些环境。启动浏览器夹具时需让 `PYTHONPATH` 包含本仓库 `src` 与 `packages/policy-sdk/src`，并使用匹配当前Playwright版本的Chromium。

本次独立性能夹具以退出码0完成统一清理，静态测试服务已停止；浏览器回归夹具正常退出，测试端口已释放。未提交或推送代码，任务账本和运行产物均未纳入版本控制。
