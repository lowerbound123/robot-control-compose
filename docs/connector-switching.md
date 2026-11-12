# 机器人 connector 热切换

RC/SDK 0.17 支持在原 RC 进程中更换机器人连接。TUI、MCP、基础设施及正常 policy 容器不重建；切换后保持暂停，等待显式 `resume` 或归还控制权。

## 配置与升级

运行 `./setup.sh --config-only` 创建缺失的 `configs/robots/*.yaml` 并增补各 policy 的公共配置。已有连接文件原样保留；本机配置不提交，模板使用 `.yaml.example`。RC 和 SDK 需同步升级，并重新创建一次原运行组以协商 `connector_switch_v1`；旧能力在破坏原状态前被拒绝。

每个命名 YAML 引用一个独立 connector 实例及设备配置。提供 mock、acone、ros-test 三份模板，也可复制 acone 模板建立多台同型机器人的连接。profile 不包含控制频率、限幅或复位目标；话题、消息类型、超时、`ros_domain_id` 和设备 `safety` 属于设备 YAML。设备 `safety` 覆盖 RC 默认限速，缺省字段使用 RC 回退值，具体规则见 [限速配置归属](connectors.md#限速配置归属)。

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

## 每个 policy 的行为

```yaml
execution:
  on_connector_switch: reinitialize_policy  # 或 reset
hot_reloadable:
  - execution.on_connector_switch
```

- 默认 `reinitialize_policy`：原进程、原模块中 release 旧 context，再 initialize 新 context。当前 Qwen 会释放并重载模型。
- `reset`：调用原 context 的 `reset_context`。当前 Qwen 清空动作、子任务或历史状态，同时保留模型。自定义策略必须完整结束旧后台任务并清除旧机器人相关状态。

该选项只影响 connector 切换，不改变普通恢复、MCP归还及已注册策略命令。可以热改并单独保存：

```text
policy example-0 config set execution.on_connector_switch reset
policy example-1 config set execution.on_connector_switch reinitialize_policy
policy example-0 config save
```

修改选项本身不初始化；RC/worker确认相同配置版本后，下次切换生效。原生chain没有该字段；重复来源按独立实例处理，保存仍写回来源目录。

`execution.on_backend_switch` 及其 `hot_reloadable`、`config_hints.settings` 路径不再接受，RC、SDK 与 setup 会在补入默认配置前明确拒绝。配置使用 `execution.on_connector_switch`，不提供别名或自动转换，schema 保持 3。

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
- 人工取消：迟到结果不得提交，已开始release按期限收尾，已提交的新连接不自动回滚；中断的policy处理需手动冷恢复。

存在未恢复实例时不能开始下一次connector切换；仍可显式 `policy switch`更换整组policy。没有自动冷重试或自动回滚。真机失去命令流后的保持/看门狗行为需要现场验收。

## 录制边界

录制中的旧会话先完成落盘，新connector提交后开启独立新会话，记录profile、连接代次、切换操作和前后会话关联。策略初始化过程可以记录在新会话内。

`record stop`取消自动重开，`record start`等新连接就绪；连接失败后的手动重试保留用户最新录制意图。旧会话收尾失败阻止关闭旧连接；新会话启动失败保留已连接connector，报告错误并保持暂停。

验收分别记录mock、本地真实worker、真实ROS、Docker、GPU模型和硬件结果，跳过不计通过。
