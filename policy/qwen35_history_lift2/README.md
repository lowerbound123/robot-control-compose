# qwen35_history_lift2

正式的独立 policy 来源，使用 LIFT2 的状态/动作统计量和夹爪输出规则。本目录独立持有入口、`src/rc_qwen35_history_lift2/` 业务实现、`vendor/` 模型源码、manifest、模型配置与部署配置；历史和调度行为与原 history 保持一致。每个运行实例仍独占容器、host、执行进程和 context。

## 归一化与夹爪

固定读取本策略所选 checkpoint 的 `stats.json["arx_lift2"]`，按左臂 6 维、左夹爪 1 维、右臂 6 维、右夹爪 1 维拼接：

- 输入状态使用 `states.*.position` 的统计量归一化。
- 模型输出逆重排后，使用 `actions.*.position` 的统计量反归一化。统计量不与状态混用。
- 归一化方式遵循该 checkpoint 的 `train_config.json`；`mean_std` 时分别执行 `(state - mean) / (std + 1e-6)` 和 `action * (std + 1e-6) + mean`。仍支持训练配置的 `q01_q99`。

LIFT2 schema 保留 history 模型的 32 维布局，两个夹爪在模型第 7、15 维。恢复原生 14 维后，delta 模式只对 12 个手臂关节加回当前状态；夹爪保持绝对值。初始化、修改模型配置和 `switch-model` 都检查 LIFT2 统计量，缺少或不合法时直接报错，不回退到 `arx_acone`，也不从其他 checkpoint 借用统计量。

随后处理输出副本的两个夹爪列（从 0 开始的第 6、13 维）：

- 原始预测值严格大于 `params.gripper_threshold`（默认 `2.5`）：输出 `4.9`。
- 小于或等于配置阈值：输出 `0`。

不调用原 history 的 `> -0.8 → 0.04` 处理。这个夹爪步骤不修改已反归一化的 12 个手臂关节，canonical 预测缓存不被阈值化；同步和流水线模式使用相同的输出规则。原 `qwen35_history` 继续使用 ACOne 统计量和夹爪规则。RC 的设备限速仍在后续发送阶段执行。

## 配置与使用

```bash
./setup.sh --policies qwen35_history_lift2 --connectors mock
```

核对本目录自己的 `deployment.env` 与 `model.json`。依赖使用已有的 `robot-control-compose-qwen35-history:local` 镜像，本目录自带 Dockerfile 和依赖锁文件，构建输入与原 history 相同；仅修改本策略入口不需要重新构建依赖镜像。

`model.json` 支持 `inference.generate_language: ["Memory", "Scene", "Subtask"]`，设为 `[]` 关闭。
这是模型冷配置，修改后用 `switch-model model.json` 重载当前实例。开启时前缀采用普通执行，去噪 CUDA Graph 仍可使用。
monitor 的 `subtask` 显示最近成功预测的 Subtask；未启用、未生成、缺少该字段或 reset/release 后返回空字符串，预热文本不展示。

本策略仍要求 `qwen3_5_context_video_ki` 类型的 history 模型，且其 `stats.json` 必须含 `arx_lift2`。历史采样和任务文本约定沿用原 history，不会自动更换模型或适配参考仓库中的旧 `qwen3_5vla_ki` 权重。依赖镜像可继续共用；源码仅挂载本目录的 `src/` 和 `vendor/`，无需为归一化切换安装新依赖。

RC 中可独立选择和配置：

```text
policy switch qwen35_history_lift2
policy qwen35_history_lift2 config show
policy qwen35_history_lift2 config set params.task "新的任务"
policy qwen35_history_lift2 config set params.gripper_threshold 3.5
policy qwen35_history_lift2 config save
```

夹爪阈值保存在本目录 `policy.yaml`，只接受有限数值。热更新不重载模型、不清空历史；
RC 配置提交推进代次后，策略丢弃旧代次的剩余计划和在途预测，从新观测重新规划并应用新阈值。
已经提交给 connector 的动作不撤回。重复实例使用精确实例名。
ACOne 和 LIFT2 使用同一个 RC 控制周期，统一配置为30Hz；修改 RC 频率需重启 RC。

切换完成后保持暂停；确认设备、模型和任务后再显式 `resume`。`switch-task`、`switch-model`、monitor、重初始化和重载接口与原 history 相同，也可将本来源放入原生 chain。切换 connector 不会自动替换 policy。

用 `monitor qwen35_history_lift2` 打开策略面板，可查看模型/任务、推理阶段与耗时、历史帧、动作缓存、请求等待、裁剪、CUDA Graph 路径和最近错误。指标与原 history 一致，详见[策略监控说明](docs/runtime.md#策略监控)；不采集显存、内存或 CPU 等资源信息。

## 定向验证

`tests/test_qwen35_history_lift2.py` 覆盖阈值边界、手臂值保留、输入与 canonical 缓存不变、原策略隔离、同步/流水线调用、独立配置和镜像复用，以及替换 GPU 后端的真实 RC/worker/example 自链。

`tests/test_qwen35_history_lift2_normalization.py` 使用刻意不同的 ACOne/LIFT2 状态与动作统计量，验证数据组选择、错误预检、模型切换以及真实 CPU 变换的归一化/逆重排/反归一化/delta 还原。测试不加载真实模型或连接硬件。

## TUI 模型参数与保存

所有模型 JSON 叶字段支持 `policy <实例> config set model_config.<section>.<field> <值>`。
`config show` 展示本实例内存快照，`config save` 写回当前模型 JSON 和策略 manifest；其他实例的内存配置不受影响。
参数生效方式、接近点融合和裁剪计数见[统一说明](../../docs/history-policy-configuration.md)。
