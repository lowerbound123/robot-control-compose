# Schema 3 动态策略容器验收

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
  -e PYTHONPATH=/workspace/src:/workspace/packages/policy-sdk/src:/workspace/packages/qwen35-policy/src:/workspace/packages/llm-direction-policy/src:/workspace/packages/llm-failure-policy/src:/workspace/packages/pi05-policy/src:/workspace/ros_ws/src/robot_control_test_sim \
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
