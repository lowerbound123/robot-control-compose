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
| `params.gripper_upper_threshold`、`gripper_upper_value`、`gripper_lower_threshold`、`gripper_lower_value` | 普通提交，保留模型及历史；已提交动作不撤回 |
| `model_config.execution.inference_timeout_s` | 普通提交，用于后续请求，原请求期限不变 |
| `model_config.execution.pipelined`、`replan_every_actions`、`model_config.transition.*` | reset，排空旧推理、清空动作及历史，保留权重 |
| `params.task`、`model_config.runtime.instruction`、`model_config.history.enabled` | reset，保留权重 |
| `model_config.model.*`、`model_config.inference.*`、`params.model_config_path` | reinitialize，重新加载模型/处理器/图缓存 |

普通提交沿用 RC 配置换代机制，旧队列及预测不会进入新配置代次，新参数从后续策略回调边界应用。

每个实例只在初始化或显式重载/切换模型文件时读取模型 JSON。TUI 修改基于该实例的内存快照；其触发的模型重新初始化仍使用候选快照。`config save` 将模型快照写回当前 `model-*.json`，manifest 参数写回 `policy.yaml`，保留相对路径。仅 set 不落盘；编辑磁盘文件不会自动改变已加载实例。A 实例保存不改变 B 实例内存，最后一次成功保存决定后续加载的文件内容。

显式 `policy <实例> switch-model model-2.json` 会读取文件，即使路径没有变化；未保存的内存模型参数因此被替换。保存目标必须位于该策略来源目录。

## 夹爪输出规则

两个策略都在轨迹融合后处理输出副本的夹爪列（第 6、13 列，从 0 开始），左右臂共用以下四个参数：

| 参数 | 含义 | qwen35_history 默认 | qwen35_history_lift2 默认 |
| --- | --- | --- | --- |
| `params.gripper_upper_threshold` | 上限 x1 | -0.8 | 2.5 |
| `params.gripper_upper_value` | 上限输出 y1 | 0.04 | 4.9 |
| `params.gripper_lower_threshold` | 下限 x2；null 禁用下限规则 | null | 2.5 |
| `params.gripper_lower_value` | 下限输出 y2 | 0.0 | 0.0 |

令处理前夹爪值为 g：若 `g >= x1`，输出 y1；否则，启用下限且 `g <= x2` 时输出 y2；均未命中则保留 g。两条规则都比较同一个处理前的值，不会对 y1/y2 再做替换。阈值相等或范围重叠时，上限规则优先。所有数值须有限，y1/y2 还须在 float32 有限范围内；仅 x2 允许 null。

例如给 LIFT2 设置一个保留中间值的区间：

```text
policy qwen35_history_lift2 config set params.gripper_upper_threshold 3.0
policy qwen35_history_lift2 config set params.gripper_upper_value 4.9
policy qwen35_history_lift2 config set params.gripper_lower_threshold 2.0
policy qwen35_history_lift2 config set params.gripper_lower_value 0.0
policy qwen35_history_lift2 config save
```

同样的字段适用于 `qwen35_history`，重复来源使用精确实例名。缺少的字段使用表中默认值，也可直接用完整字段名 `config set` 添加；含旧名称的配置须先按下一节迁移。四项都支持普通热更新和保存，不重载模型或清空历史；配置换代后从新观测重新规划。缓存直返不重复处理夹爪，原始预测和 canonical 缓存不受输出替换影响，LIFT2 的原始夹爪 monitor 仍展示处理前的完整模型预测。

比较包含等号：默认 LIFT2 在 g 恰为 2.5 时输出 4.9；默认原 history 在 g 恰为 -0.8 时输出 0.04。此次统一命名保持这些比较规则与默认值不变。

## 夹爪参数改名升级

`qwen35_history`、`qwen35_history_lift2` 和 `pi05_lift2` 统一使用 `upper/lower` 和 `threshold/value` 两组名称：

| 旧 params 字段 | 新 params 字段 |
| --- | --- |
| `gripper_threshold` | `gripper_upper_threshold` |
| `gripper_high_value` | `gripper_upper_value` |
| `gripper_low_threshold` | `gripper_lower_threshold` |
| `gripper_low_value` | `gripper_lower_value` |

更新源码后，在项目根目录用安装过 `requirements/setup.lock` 的宿主 Python 执行：

```bash
python3 scripts/migrate_gripper_params.py          # 预览，不修改文件
python3 scripts/migrate_gripper_params.py --apply  # 备份后改名
```

脚本只处理这三个来源已有的 `policy.yaml`，保留原值，并同步改名热更新白名单和配置提示；原文件备份到 `.setup/backups/`。它不会创建缺失的策略配置或重载进程。新旧字段同时存在且值不同时会报错，所有文件预检完成后才开始写入；重复执行不产生额外改动。`./setup.sh --config-only` 也会先迁移这些名字，再合并模板默认值。

运行时不再接受旧名称，避免旧自定义值被忽略后回退到默认值。脚本中的命令也要改成新名称。运行中的实例需通过现有流程重新加载策略源码，不能用仅重新初始化 context 的操作代替；迁移本身不改变已加载实例的参数。本次命名修改无需重建依赖镜像。

## 暂停、恢复与复位

两个 history 来源在 `pause → resume` 和 MCP 接管后归还时保留全部已消费视觉历史，恢复时只排空旧推理并清除旧动作计划。暂停期间不追加历史；恢复后从原消费进度继续积累。

显式 `reset` 在复位目标完整发布确认后清空当前 history 实例的历史帧、历史时钟和消费基准，并保持暂停。命令成功回执会等待策略清理完成，不需要再执行 `resume` 才清空；下一次恢复从空历史开始。失败或取消不报告清理成功。原生链中的每个 history 实例独立清理。

任务修改、要求 reset 的模型配置更新、重初始化与重载仍按各自生命周期清空历史，模型权重仅在需要重新初始化时重新加载。该恢复/复位区分需要 RC、公共 SDK 和策略源码一并更新。

## 流水线与融合

RC 保持 30 Hz，`execution.mode` 接受 `sync` / `async`，`execution.call_every_actions` 接受正整数。它们分别决定等待策略时使用 hold 还是继续提交缓存动作、多久调用一次策略；不会替 policy 决定是否推理。目标只有收到完整发布确认才算消费；已开始的分步目标保留，sync 策略等待期间保持最近提交的位置，等待结束后再继续限速推进。

policy 每次查看传入的剩余动作数量：大于 `50-X` 时原样返回，不推理、不重复后处理；小于或等于此阈值时执行一轮推理。X 是现有 `model_config.execution.replan_every_actions`，例如 X=45 表示剩余 5 条时推理。RC 调用间隔和 policy 推理阈值是两项独立配置，现有值不会自动互相覆盖。

`pipelined=false` 直接返回完整新预测；`true` 将请求中收到的旧 chunk 与新预测融合后返回。这个开关不再启动另一套后台调度。需要推理的 policy 调用会等待本轮结果；外层 async 时 RC 同时继续提交旧缓存，sync 时按周期重复 hold、等待返回。缓存耗尽时两种模式都发送 hold；hold 本身不增加已消费动作数。

policy 使用 SDK `TrajectoryAddTransition` 寻找接近点并融合，默认用旧段尾点寻找新段最近点，全部 14 维（包含夹爪）参与距离和插值；之后执行夹爪处理。回包保留请求时的起点，由 RC 单独裁掉已经消费的前缀、保留已提交目标并更新缓存，不重复裁剪。融合后的缓存长度可能超过 50，按实际长度消费。

| `transition` 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `trunk_mode` | `tail` | `tail` 旧尾点匹配新段；`close` 两段全局最近点；`now` 旧段第三点匹配，短段回退尾点 |
| `trunk_smoothing` | `true` | 衔接插值；`now` 沿用 SDK 的直接接续行为 |
| `max_smoothing_steps` | `-1` | 融合窗口不限；正整数限制窗口 |
| `exponential_factor` | `5.0` | 正数，指数融合因子 |
| `exponential_smoothing` | `0.25` | 正数，指数平滑系数 |

RC 同时只保留一个在途策略请求。返回结果全部过时时，由 RC 丢弃结果并保留有效缓存；缓存耗尽时等待新结果。超时、非有限输出和真实推理异常仍进入故障处理。历史由独立 observe 通道更新，模型使用请求消费时刻的历史快照。
