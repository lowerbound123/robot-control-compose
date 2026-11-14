# History 策略配置与执行

适用于 `qwen35_history`、`qwen35_history_lift2`。重复来源使用确切实例名，例如 `qwen35_history-0`。

## TUI 修改与保存

`model_config.<section>.<field>` 对应当前所选 `model-*.json` 的叶字段；列表整体输入 JSON。`params.*` 对应策略 manifest。

```text
policy qwen35_history config show
policy qwen35_history config set execution.mode async
policy qwen35_history config set execution.call_every_actions 1
policy qwen35_history config set model_config.execution.pipelined true
policy qwen35_history config set model_config.execution.replan_every_actions 45
policy qwen35_history config set model_config.inference.generate_language ["Memory","Scene","Subtask"]
policy qwen35_history config save
```

`config show` 展示内存中的模型参数、manifest 参数、`effective_task` 和 `task_source`。`params.task` 非 null 时覆盖 `model_config.runtime.instruction`。配置提交回执含 `configuration_effect`，日志说明生效方式；reset/reinitialize 完成后保持暂停，显式 `resume` 才恢复执行。

| 设置 | 生效方式 |
| --- | --- |
| `execution.mode`、`execution.call_every_actions` | RC 调用调度更新，保留动作缓存、动作代次和策略上下文；仍确认配置版本 |
| `params.gripper_threshold` | 普通提交，保留模型及历史；已提交动作不撤回 |
| `model_config.execution.inference_timeout_s` | 普通提交，用于后续请求，原请求期限不变 |
| `model_config.execution.pipelined`、`replan_every_actions`、`model_config.transition.*` | reset，排空旧推理、清空动作及历史，保留权重 |
| `params.task`、`model_config.runtime.instruction`、`model_config.history.enabled` | reset，保留权重 |
| `model_config.model.*`、`model_config.inference.*`、`params.model_config_path` | reinitialize，重新加载模型/处理器/图缓存 |

普通提交沿用 RC 配置换代机制，旧队列及预测不会进入新配置代次，新参数从后续策略回调边界应用。

每个实例只在初始化或显式重载/切换模型文件时读取模型 JSON。TUI 修改基于该实例的内存快照；其触发的模型重新初始化仍使用候选快照。`config save` 将模型快照写回当前 `model-*.json`，manifest 参数写回 `policy.yaml`，保留相对路径。仅 set 不落盘；编辑磁盘文件不会自动改变已加载实例。A 实例保存不改变 B 实例内存，最后一次成功保存决定后续加载的文件内容。

显式 `policy <实例> switch-model model-2.json` 会读取文件，即使路径没有变化；未保存的内存模型参数因此被替换。保存目标必须位于该策略来源目录。

## 流水线与融合

RC 保持 30 Hz，`execution.mode` 接受 `sync` / `async`，`execution.call_every_actions` 接受正整数。它们分别决定 RC 是否等待策略返回、多久调用一次策略；不会替 policy 决定是否推理。目标只有收到完整发布确认才算消费，已开始的分步目标继续完成。

policy 每次查看传入的剩余动作数量：大于 `50-X` 时原样返回，不推理、不重复后处理；小于或等于此阈值时执行一轮推理。X 是现有 `model_config.execution.replan_every_actions`，例如 X=45 表示剩余 5 条时推理。RC 调用间隔和 policy 推理阈值是两项独立配置，现有值不会自动互相覆盖。

`pipelined=false` 直接返回完整新预测；`true` 将请求中收到的旧 chunk 与新预测融合后返回。这个开关不再启动另一套后台调度。需要推理的 policy 调用会等待本轮结果；外层 async 时 RC 同时继续消费旧缓存，sync 时等待返回。

policy 使用 SDK `TrajectoryAddTransition` 寻找接近点并融合，默认用旧段尾点寻找新段最近点，全部 14 维（包含夹爪）参与距离和插值；之后执行夹爪处理。回包保留请求时的起点，由 RC 单独裁掉已经消费的前缀、保留已提交目标并更新缓存，不重复裁剪。融合后的缓存长度可能超过 50，按实际长度消费。

| `transition` 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `trunk_mode` | `tail` | `tail` 旧尾点匹配新段；`close` 两段全局最近点；`now` 旧段第三点匹配，短段回退尾点 |
| `trunk_smoothing` | `true` | 衔接插值；`now` 沿用 SDK 的直接接续行为 |
| `max_smoothing_steps` | `-1` | 融合窗口不限；正整数限制窗口 |
| `exponential_factor` | `5.0` | 正数，指数融合因子 |
| `exponential_smoothing` | `0.25` | 正数，指数平滑系数 |

RC 同时只保留一个在途策略请求。返回结果全部过时时，由 RC 丢弃结果并保留有效缓存；缓存耗尽时等待新结果。超时、非有限输出和真实推理异常仍进入故障处理。历史由独立 observe 通道更新，模型使用请求消费时刻的历史快照。
