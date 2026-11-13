# History 策略配置与执行

适用于 `qwen35_history`、`qwen35_history_lift2`、`qwen35_history_clip`。重复来源使用确切实例名，例如 `qwen35_history-0`。

## TUI 修改与保存

`model_config.<section>.<field>` 对应当前所选 `model-*.json` 的叶字段；列表整体输入 JSON。`params.*` 对应策略 manifest。

```text
policy qwen35_history config show
policy qwen35_history config set model_config.execution.pipelined true
policy qwen35_history config set model_config.execution.replan_every_actions 45
policy qwen35_history config set model_config.inference.generate_language ["Memory","Scene","Subtask"]
policy qwen35_history config save
```

`config show` 展示内存中的模型参数、manifest 参数、`effective_task` 和 `task_source`。`params.task` 非 null 时覆盖 `model_config.runtime.instruction`。配置提交回执含 `configuration_effect`，日志说明生效方式；reset/reinitialize 完成后保持暂停，显式 `resume` 才恢复执行。

| 设置 | 生效方式 |
| --- | --- |
| `params.gripper_threshold`、clip 的 `params.arm_clip_enabled` / `params.arm_clip_max_delta_rad` | 普通提交，保留模型及历史；已提交动作不撤回 |
| `model_config.execution.inference_timeout_s` | 普通提交，用于后续请求，原请求期限不变 |
| `model_config.execution.pipelined`、`replan_every_actions`、`model_config.transition.*` | reset，排空旧推理、清空动作及历史，保留权重 |
| `params.task`、`model_config.runtime.instruction`、`model_config.history.enabled` | reset，保留权重 |
| `model_config.model.*`、`model_config.inference.*`、`params.model_config_path` | reinitialize，重新加载模型/处理器/图缓存 |

普通提交沿用 RC 配置换代机制，旧队列及预测不会进入新配置代次，新参数从后续策略回调边界应用。

每个实例只在初始化或显式重载/切换模型文件时读取模型 JSON。TUI 修改基于该实例的内存快照；其触发的模型重新初始化仍使用候选快照。`config save` 将模型快照写回当前 `model-*.json`，manifest 参数写回 `policy.yaml`，保留相对路径。仅 set 不落盘；编辑磁盘文件不会自动改变已加载实例。A 实例保存不改变 B 实例内存，最后一次成功保存决定后续加载的文件内容。

显式 `policy <实例> switch-model model-2.json` 会读取文件，即使路径没有变化；未保存的内存模型参数因此被替换。保存目标必须位于该策略来源目录。

## 流水线与融合

RC 保持 30 Hz；manifest 保持 `execution.mode=sync`、`call_every_actions=1`、`control_mode=qpos`。完整目标等待发布确认后才消费并进入回调；已开始的分步目标继续完成，不因重复请求空转。

`pipelined=false` 时，预测 50 条、执行前 X 条，再同步等待下一批。`pipelined=true` 时，X 是重规划触发间隔，不是旧缓存执行上限；新推理未完成时继续消费余量。初始缓存 50 条、X=45 时，第 45 条消费后启动推理，第 46～50 条仍可执行。

新结果先按实际消费数裁掉过时前缀，再使用 SDK `TrajectoryAddTransition` 寻找接近点并融合。默认用旧段尾点寻找新段最近点，全部 14 维（包含夹爪）参与距离和插值；之后执行各策略的夹爪阈值处理，clip 最后执行臂关节裁剪。融合后的缓存长度可能超过 50，按实际长度消费。

| `transition` 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `trunk_mode` | `tail` | `tail` 旧尾点匹配新段；`close` 两段全局最近点；`now` 旧段第三点匹配，短段回退尾点 |
| `trunk_smoothing` | `true` | 衔接插值；`now` 沿用 SDK 的直接接续行为 |
| `max_smoothing_steps` | `-1` | 融合窗口不限；正整数限制窗口 |
| `exponential_factor` | `5.0` | 正数，指数融合因子 |
| `exponential_smoothing` | `0.25` | 正数，指数平滑系数 |

同时最多一个后台推理。完全过时的新结果被丢弃，并从最新观测重新推理；有效旧缓存可继续执行，缓存耗尽时等待。超时、非有限输出和真实推理异常仍进入故障处理，不自动恢复。

## Clip 的阈值与计数

```text
policy qwen35_history_clip config set params.arm_clip_enabled false
policy qwen35_history_clip config set params.arm_clip_enabled true
policy qwen35_history_clip config set params.arm_clip_max_delta_rad 0.08
policy qwen35_history_clip config save
monitor qwen35_history_clip
```

阈值默认 `0.1 rad`，要求有限且大于零；只裁剪 12 个臂关节。monitor 提供：

- `arm_clip_enabled`：硬裁剪开关，默认 `true`，支持热更新；关闭期间新产生的动作不触发硬裁剪。
- `arm_clip_max_delta_rad`：当前阈值。
- `arm_clip_count`：已消费动作中至少一个臂关节超过阈值的动作条数。
- `arm_clip_joint_count`：已消费动作中超过阈值的关节总次数。

计数依据后续回调报告的 `consumed_actions` 更新，不统计未执行预测，也不因重复返回缓存而累加。普通 reset 重建消费游标但保留累计值，实例重新初始化/重载后清零。裁剪改变目标本身，不追加动作补回原预测终点。

切换硬裁剪开关保留模型、历史和累计计数。关闭后继续跟踪实际消费目标，重新开启时沿用最后一条已消费目标作为锚点；RC 的设备限速始终生效。
