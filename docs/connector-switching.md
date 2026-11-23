# 机器人 connector 热切换

RC/SDK 0.17 支持在原 RC 进程中更换机器人连接。TUI、MCP、基础设施及正常 policy 容器不重建；切换后保持暂停，等待显式 `resume` 或归还控制权。

## 配置与升级

运行 `./setup.sh --config-only` 创建缺失的 `configs/robots/*.yaml` 并清理旧 policy 配置中的连接切换选项。已有连接文件原样保留；本机配置不提交，模板使用 `.yaml.example`。RC 和 SDK 需同步升级，并重新创建一次原运行组以协商 `connector_switch_v1`；旧能力在破坏原状态前被拒绝。

每个命名 YAML 引用一个独立 connector 实例及设备配置，也可复制模板建立多台同型机器人的连接。话题、消息类型、超时、`ros_domain_id`、设备限速 `safety` 和复位基准 `reset_qpos` 写在设备 YAML 中。切换成功后使用新设备的基准，设备缺省时回到全零。设备 `safety` 覆盖 RC 默认限速，具体规则见 [设备复位基准和限速](connectors.md#设备复位基准)。

```yaml
# configs/robots/acone.yaml
connector: body
source: acone
config: devices/robot-main.yaml
```

`connector` 是运行实例身份，`source` 是 connector 来源目录，`config` 相对于该来源目录解析，也可引用 RC 容器内可读的绝对文件路径。多个 profile 可以共用来源目录及镜像。

`robot_profiles_directory` 相对于 RC 文件解析，`configs/rc.*.yaml` 使用 `robots`，根目录 `rc.yaml` 使用 `configs/robots`。命名启动配置：

```yaml
connector_directory: ../connector
robot_profiles_directory: robots
robot:
  profile: acone
```

旧 `robot.backend` 内联配置被明确拒绝。直接启动使用 `robot.connector` 和 `connectors` 声明；命名启动使用 `robot.profile`，不能与 `robot.connector` 混写。切换不落盘，`config rc save` 才保存当前命名选择，不修改连接文件。名称最多48字符，只接受小写字母、数字、下划线和连字符；不接受路径或符号链接。每次显式切换重新读取文件，同名切换也重新连接。

切换前须通过 `./setup.sh --connectors mock,acone --policies example` 准备所需镜像。网络、ROS 消息包、依赖和设备权限由 connector 来源的 Compose 管理；ACOne connector 使用 host 网络，RC 自身保持无 ROS 的基础网络环境。设备配置中的 `ros_domain_id` 决定新 connector 的 ROS 域。项目仍不启动厂商硬件驱动。

## 切换后的 policy

`connector switch` 对当前策略或链中的每个实例调用一次 `reset_context`。实例保留原容器、进程、context、配置版本和已加载的模型。策略须在回调返回前结束旧推理、丢弃旧动作和观测历史。此回调仅清理策略状态，机器人保持新连接反馈的当前位置。

从 ACOne 切换到 LIFT2 也遵循同一规则。切换完成后保持暂停。要更换模型对应的策略，显式执行 `policy switch <策略名>`，例如 `policy switch qwen35_history_lift2`。继续使用当前策略时可直接执行 `resume`。

RC 加载旧 schema 3 配置时会忽略 `execution.on_connector_switch` 的两个旧值及其补全提示和热改白名单项。旧值 `reinitialize_policy` 也会走上述 reset 流程。保存策略配置或运行 `./setup.sh --config-only` 会清除这些旧项。

## 操作、状态和恢复

```text
connector list
connector status
connector switch acone
resume
```

MCP使用只读 `connector_profiles` 查询。先 `take`，再调用 `switch_connector(profile="acone")`。`retry_connector` 用于用户明确要求的连接恢复。写工具支持 `expected_control`、操作回执和可选等待；HTTP超时/断连不取消已受理事务，不应自动重发。无连接时可以接管用于恢复，运动入口不可用。

`connector list` 在终端显示名称、来源、实例、配置路径和有效性；无效配置逐行显示错误，查询失败也输出错误。MCP profile 行使用 `connector` 表示实例，`source` 表示来源。

切换先预检查设备参数（包括限速），再暂停推理与手动动作，收齐全链本地及外部图像读者确认。旧 connector 先排空已提交动作，再确认进程树、容器和 reader 退出；确认前不创建新 connector，关闭期间不发送新目标；新双臂反馈与三路图像就绪后，同步安装新设备的有效限速并从新机器人实际位置建立保持目标，再逐实例处理policy。旧动作、图像、限速起点和迟到预览结果不得进入新连接。

`status.connector` 与 `props.connector` 显示当前/目标profile、连接代次、阶段、错误和逐实例结果。状态观测与图像使用 `connector_generation` 标记连接代次。MCP操作保留控制权；所有成功路径都保持暂停，不自动复位或恢复运动。

- 预检查失败：原连接、context、录制会话不变。
- 旧读者/连接退出未确认：阻断新连接，保留清理对象与证据，断连不能替代退出证明。
- 新连接失败：无动作输出，policy保留并暂停。修正文件后执行 `connector retry`，先完成未确认清理，再重新读取目标。
- policy局部失败：继续其他实例，新机器人只保持当前位置，整链不恢复。`retry_reload_instances`列出失败实例；用 `policy <实例> retry`／MCP `retry_reload`逐个冷重试，健康实例不重复处理。
- 人工取消：迟到结果不得提交，已提交的新连接不自动回滚；中断的 policy reset 需手动冷恢复。

存在未恢复实例时不能开始下一次connector切换；仍可显式 `policy switch`更换整组policy。没有自动冷重试或自动回滚。真机失去命令流后的保持/看门狗行为需要现场验收。

## 录制边界

录制中的旧会话先完成落盘，新connector提交后开启独立新会话，记录profile、连接代次、切换操作和前后会话关联。新会话涵盖策略状态重置阶段。

`record stop`取消自动重开，`record start`等新连接就绪；连接失败后的手动重试保留用户最新录制意图。旧会话收尾失败阻止关闭旧连接；新会话启动失败保留已连接connector，报告错误并保持暂停。

验收分别记录mock、本地真实worker、真实ROS、Docker、GPU模型和硬件结果，跳过不计通过。
