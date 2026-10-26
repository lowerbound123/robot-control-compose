# Policy 信息监控验证记录

验证日期：2026-10-23。两仓主程序和 SDK 为 0.8.0。本轮修改保留原有未提交功能；未提交、合并或推送。

## 最终验证结果

| 验证 | 结果 | 完整日志 |
|---|---|---|
| 原项目本机完整回归，Python 3.11.17 | 579 passed / 20 skipped，160.58 s | `/private/tmp/rc-monitor-source-final.log` |
| Compose 项目本机完整回归，Python 3.11.17 | 564 passed / 65 skipped，163.87 s | `/private/tmp/rc-monitor-compose-final.log` |
| Humble 镜像完整回归，Python 3.10.12 | 541 passed / 58 skipped，177.23 s | `/private/tmp/rc-monitor-linux-final.log` |
| Python 3.10 RC → Python 3.14/3.11 容器监控 | 3 passed，4.55 s | `/private/tmp/rc-monitor-cross-final.log` |
| 原有跨容器三路图像、策略链及独立 ROS 模拟器闭环 | 6 passed，5.18 s | `/private/tmp/rc-monitor-ros-final.log` |
| 监控挂起时 Compose 全栈停止 | 6.25 s；退出码 1（强制清理）；读者为零；帧池零字节 | `/private/tmp/rc-monitor-stop-final.log` |
| 默认 mock + hold 部署 | 启动暂停、监控关闭、resume/pause 正常、退出码 0、帧池归零 | `/private/tmp/rc-monitor-default.log` |
| 两个本地测试环境及三个业务镜像依赖检查 | 全部通过 | `/private/tmp/rc-monitor-source-install.log`、`/private/tmp/rc-monitor-compose-install.log` |

完整回归均为退出码 0。13 条警告来自已有 Pillow `getdata()` 弃用提示。跳过项主要是生产 Conda、当前平台缺失的 ROS/Linux 能力、Torch/GPU、旧进程 API 和需另行启动的专项 Compose 配置；专门的监控跨容器和 ROS 闭环已按上表单独执行。

## 覆盖的关键行为

- 精确标量类型、有限浮点数、128 项/64 KiB 限额、字典完整替换和空数据。
- 每实例独立线程和单个在途请求；长推理期间采集；监控与控制队列容量隔离，控制回包受背压时等待而不丢弃。
- 1 Hz 控制频率下仍以 10 Hz 目标周期采集；关闭再开启、旧结果过滤、MCP 接管/归还、重置、热配置和同名重载。
- 挂起回调在普通运行期间只影响显示；释放等待超时后清理旧进程树，图像读者退出确认保持独立。
- `A → B → A` 的实例和面板去重，同 host / 不同 host 状态隔离。监控结果不会释放仍在借用的图像槽。
- 命令帮助、补全、前缀保留、headless/MCP 边界、右侧布局、长中文/控制字符、自动高度、滚动和缩放。
- Textual 先停止消息循环、再卸载子组件的退出顺序；定时刷新在该阶段不再查询已移除组件。
- ready 原子发布 monitor/observe 能力；即使后续 capabilities 消息延迟，链也不会漏掉已就绪的观察者。

## 构建产物

本机构建并验证的是 Linux ARM64 镜像；x86 主机应按项目教程重新构建相应架构。三个镜像的 SDK 均为 0.8.0，已逐文件核对安装后的 SDK 与主程序实现哈希，匹配最终工作区源码。

| 镜像 | Python | 架构 | Image ID |
|---|---|---|---|
| robot-control-compose-policy:local | 3.14.9 | aarch64 | `sha256:f7f4987aec0d9aaa0418b896ab97d55ee55aba1e2f1d0813f17a00636951caef` |
| robot-control-compose-llm-direction:local | 3.11.17 | aarch64 | `sha256:1dd0204e7890d43c91341bbc6675aae3f798ce7340dccaef0086d00a8ebcee71` |
| robot-control-compose-rc:local | 3.10.12 | aarch64 | `sha256:43b7d259d8264c4323d22fcd5a6f663e83252b09314957674401f5cc37b33db1` |

SDK/RC 的依赖未增加模型框架。Qwen 镜像本轮未重建；若部署启用了 Qwen，同样需要按教程重建其镜像以安装 SDK 0.8。

## 审阅与边界

独立只读审阅发现 1 项重要问题：原项目包版本和 SDK 最低依赖未同步升级。已修复，并完成 0.8.0 可安装性、元数据和依赖一致性检查；无未处理审阅项。回归中发现的观察错误回执、TUI 卸载时序和 ready 能力协商问题均已用失败用例复现并修复，最终完整套件通过。

原项目真实 worker 采用测试解释器注入，不等同于生产 Conda 安装验收。没有调用真实 LLM 供应商、加载 GPU checkpoint 或操作实体机器人，也未做 Ghostty 的人工视觉验收。此功能的业务指标由 policy 作者选择，现有 hold/Qwen/LLM 策略未自动加入 monitor 字段。
