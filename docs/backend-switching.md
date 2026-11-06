# 机器人 backend 热切换

RC/SDK 0.15 支持在原 RC 进程中更换机器人连接。TUI、MCP、基础设施及正常 policy 容器不重建；切换后保持暂停，等待显式 `resume` 或归还控制权。

## 配置与升级

运行 `./setup.sh --config-only` 创建缺失的 `configs/robots/*.yaml` 并增补各 policy 的公共配置。已有连接文件原样保留；本机配置不提交，模板使用 `.yaml.example`。RC 和 SDK 需同步升级，并重新创建一次原运行组以协商 `backend_switch_v1`；旧能力在破坏原状态前被拒绝。

每个命名 YAML 保存原 `robot` 块的连接参数。提供 mock、acone、ros-test 三份模板，也可复制 acone 模板建立多台同型机器人的连接。文件只描述 backend、话题、消息类型、超时和可选 `domain_id`；不包含控制频率、限幅或复位目标。

`robot_profiles_directory` 相对于 RC 文件解析，`configs/rc.*.yaml` 使用 `robots`，根目录 `rc.yaml` 使用 `configs/robots`。命名启动配置：

```yaml
robot_profiles_directory: robots
robot:
  profile: acone
```

旧 `robot.backend` 内联配置仍可启动；不能与 `profile` 混写。已有模板保留内联启动方式。切换不落盘，`config rc save` 才保存当前命名选择，不修改连接文件。名称最多48字符，只接受小写字母、数字、下划线和连字符；不接受路径或符号链接。每次显式切换重新读取文件，同名切换也重新连接。

网络、ROS消息包、依赖和设备权限须在启动时就绪。默认 mock Compose 网络不保证能发现宿主真机；需要 mock↔真机切换时，以真机的 host 网络方式启动，再在 RC 配置选择初始 profile。`domain_id` 显式传入新 ROS Context，省略时沿用环境。项目仍不启动厂商硬件驱动。

## 每个 policy 的行为

```yaml
execution:
  on_backend_switch: reinitialize_policy  # 或 reset
hot_reloadable:
  - execution.on_backend_switch
```

- 默认 `reinitialize_policy`：原进程、原模块中 release 旧 context，再 initialize 新 context。当前 Qwen 会释放并重载模型。
- `reset`：调用原 context 的 `reset_context`。当前 Qwen 清空动作、子任务或历史状态，同时保留模型。自定义策略必须完整结束旧后台任务并清除旧机器人相关状态。

该选项只影响 backend 切换，不改变普通恢复、MCP归还及已注册策略命令。可以热改并单独保存：

```text
policy example-0 config set execution.on_backend_switch reset
policy example-1 config set execution.on_backend_switch reinitialize_policy
policy example-0 config save
```

修改选项本身不初始化；RC/worker确认相同配置版本后，下次切换生效。原生chain没有该字段；重复来源按独立实例处理，保存仍写回来源目录。

## 操作、状态和恢复

```text
backend list
backend status
backend switch acone
resume
```

MCP使用只读 `backend_profiles` 查询。先 `take`，再调用 `switch_backend(profile="acone")`。`retry_backend` 用于用户明确要求的连接恢复。写工具支持 `expected_control`、操作回执和可选等待；HTTP超时/断连不取消已受理事务，不应自动重发。无连接时可以接管用于恢复，运动入口不可用。

切换先预检查，再暂停推理与手动动作，收齐全链本地及外部图像读者确认。旧连接关闭期间不发送目标；新双臂反馈与三路图像就绪后，从新机器人实际位置建立保持目标，再逐实例处理policy。旧动作、图像和迟到预览结果不得进入新连接。

`status.backend` 显示当前/目标profile、连接代次、阶段、错误和逐实例结果。MCP操作保留控制权；所有成功路径都保持暂停，不自动复位或恢复运动。

- 预检查失败：原连接、context、录制会话不变。
- 旧读者/连接退出未确认：阻断新连接，保留清理对象与证据，断连不能替代退出证明。
- 新连接失败：无动作输出，policy保留并暂停。修正文件后执行 `backend retry`，先完成未确认清理，再重新读取目标。
- policy局部失败：继续其他实例，新机器人只保持当前位置，整链不恢复。`retry_reload_instances`列出失败实例；用 `policy <实例> retry`／MCP `retry_reload`逐个冷重试，健康实例不重复处理。
- 人工取消：迟到结果不得提交，已开始release按期限收尾，已提交的新连接不自动回滚；中断的policy处理需手动冷恢复。

存在未恢复实例时不能开始下一次backend切换；仍可显式 `policy switch`更换整组policy。没有自动冷重试或自动回滚。真机失去命令流后的保持/看门狗行为需要现场验收。

## 录制边界

录制中的旧会话先完成落盘，新backend提交后开启独立新会话，记录profile、连接代次、切换操作和前后会话关联。策略初始化过程可以记录在新会话内。

`record stop`取消自动重开，`record start`等新连接就绪；连接失败后的手动重试保留用户最新录制意图。旧会话收尾失败阻止关闭旧连接；新会话启动失败保留已连接backend，报告错误并保持暂停。

验收分别记录mock、本地真实worker、真实ROS、Docker、GPU模型和硬件结果，跳过不计通过。
