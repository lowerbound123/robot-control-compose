# LLM direction 软件验收记录

日期：2026-10-23。两个仓库分别包含独立的同名策略和相同适配包；未依赖运行时跨仓库导入。

## 已执行

| 检查 | 结果 |
| --- | --- |
| 原 robot-control 完整套件，Python 3.11.17 | **546 passed、20 skipped、13 warnings**，135.08 s |
| Compose 完整 Linux/Humble 主镜像套件，Python 3.10 | **507 passed、55 skipped、13 warnings**，156.82 s |
| Compose 可选 LLM 环境：动作、供应商、真实 worker | **62 passed、6 skipped**；跳过的是原 Conda launcher 专属检查 |
| Python 3.10 RC → amd64 Python 3.11 LLM host | **9 passed**，22.53 s |
| 既有跨容器 hold + 真实 rclpy/独立 ROS 模拟器闭环 | **6 passed**，4.69 s |
| 默认 mock + hold，无 OpenAI SDK、GPU、API key | 暂停启动与正常停止通过 |
| Compose stop 时存在真实在途 HTTP 请求 | 控制器和 policy host 均退出 **0**，隔离项目清理完成 |
| Compose 配置组合 | 默认、LLM、LLM+ROS、LLM+ACOne、LLM+Qwen 均可解析 |
| 镜像依赖及安装包 | Linux amd64 构建、pip check、site-packages 安装位置检查通过 |
| 两仓库一致性 | 7 个适配包源文件一致；17 项共有依赖锁版本一致，Compose 另外包含 posix-ipc |
| 原项目已有改动 | 扣除本次文档追加及 launcher 环境变量代码后，既有文件哈希与实施前完全一致 |

新策略回归覆盖全部方向与 MCP 的一致性、非零初始姿态、局部旋转、Slerp、双手动作、夹爪、noop、阈值边界、前缀保留、尾部锚点、完成暂停和 A/B/A。HTTP 测试使用真实官方 SDK 与本地 HTTP 服务，覆盖四种接口、三种自定义输出格式、三图编码、联合选择、拒答、非法 JSON、重复键、超时、断连、零自动重试及错误脱敏。

跨容器验收使用真正的 UDS 和 POSIX 共享内存，由 Python 3.10 发布三路 RGB，amd64 Python 3.11 读取并编码。HTTP 端逐像素检查 JPEG 内容（允许 JPEG 的 1 灰度级误差），验证 UDS 消息不含图像负载。长请求、硬超时、取消、MCP 接管和冷重载均验证图像释放证明与迟到结果丢弃。

## 镜像与依赖

镜像标签为 `robot-control-compose-llm-direction:local`，Linux amd64，约 585 MB。此轮镜像标识为 `sha256:d3e587d15a06f01323365f1fad438211d916d91853fa6cc9324e7c2018e9c9d3`。

| 组件 | 验证版本 |
| --- | --- |
| Python | 3.11.17 |
| OpenAI SDK | 3.37.0 |
| NumPy | 2.2.6 |
| Pillow | 13.0.0 |
| SciPy | 1.17.1 |
| posix-ipc（Compose） | 1.3.2 |

核心依赖锁含完整传递依赖及哈希；两个原生测试环境的依赖一致性检查也通过。镜像健康检查和构建检查没有访问真实 LLM。

## 审查与复现

独立只读审查没有发现动作转换、链式缓存、供应商协议或图像租约方面的阻断问题。审查指出声明 API key 时可能误用保留环境变量；已在原 launcher 启动前及两份配置校验中拒绝 Python/Conda/ROS/动态链接器保留名，并用 10 个先失败后通过的回归检查验证。Decisions 的输出提示单独使用联合选择说明。

首轮原项目全量运行因临时环境缺少旧 model-server bridge 的可选依赖失败，补齐独立 bridge 解释器与 model-client 后，7 项 bridge 集成和最终全量通过。一项既有 TUI 退出时序检查首轮失败，单项复跑及最终全量均通过，没有修改 TUI。13 条警告来自现有 textual-image 对 Pillow getdata() 的弃用调用。

本机有效日志位于 `/private/tmp/llm-source-full-final.log`、`/private/tmp/llm-compose-linux-full.log`、`/private/tmp/llm-compose-reviewed.log`、`/private/tmp/llm-direction-cross-final.log`、`/private/tmp/llm-existing-ros-integration.log` 和 `/private/tmp/llm-direction-compose-stop.log`。这些临时路径不属于交付依赖。

复现单元、worker 和跨容器测试的命令见 [使用教程](llm-direction-policy.md)。另外，在 robot-control-compose 根目录构建测试镜像后，可运行 `python3 tests/llm_compose_stop.py`。该脚本创建唯一项目，通过容器内真实 MCP take/return 恢复 mock policy，确认慢 HTTP 请求已开始，然后执行 Compose stop；结束时自动删除自己的容器和测试卷。

## 未执行的部署验收

没有调用真实 DeepSeek/OpenAI/自定义供应商，没有检查真实账号的模型权限、限额或计费。原项目本地 worker 验证使用指定 Python 3.11 解释器，实际 Conda 安装仍应在部署机验收。实体机器人、真实任务完成质量、不同相机布局下的方向理解和实际动作效果需要单独验收。

全量中的跳过项包含实际 Conda、部分 ROS 部署组合、缺少 Torch 的 Qwen 检查，以及要求专用 Compose 项目的检查。LLM 的 9 项专用跨容器检查和既有 6 项 ROS/跨容器检查已另外运行；没有用跳过或 HTTP 替身来声称真实供应商、CUDA 模型或实体机器人验收通过。
