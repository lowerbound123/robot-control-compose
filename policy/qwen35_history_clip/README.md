# qwen35_history_clip

从 `qwen35_history` 复制的临时 ACOne 策略。入口、`src/rc_qwen35_history_clip/`、
`vendor/`、构建输入和配置全部归本目录，不从另一个 policy 导入业务代码。
每个运行实例独占容器、host 和执行进程，复用已有的 Qwen history 依赖镜像。

## 动作裁剪

沿用 ACOne 的 14 维双臂布局、归一化和历史推理，在输出前额外应用 `infer_a2.py`
的臂关节裁剪：返回 chunk 内每个臂关节相对上一条裁剪目标最多变化 `±params.arm_clip_max_delta_rad`（默认 `0.1 rad`），两个夹爪不参与裁剪。
`params.arm_clip_enabled` 控制是否启用硬裁剪，默认 `true`，支持布尔值热更新，不重载权重或清空累计计数。
关闭时直接输出夹爪后处理后的新预测。新 chunk 的首条以本次请求观测的 `qpos` 为参考，后续以该 chunk 内上一条裁剪目标为参考。
仅保证返回 chunk 内部的限幅；RC 裁掉过时前缀后，新缓存首条相对上一条实际执行目标可能超过该阈值。RC 不做第二次硬裁剪，设备限速仍生效。
剩余量充足时原样返回输入旧 chunk，不重新裁剪，也不生成新的裁剪标记。

每条目标被裁剪后的值替代；不会追加动作去补齐原始期望终点，因此最终位置可能与原预测不同。
RC 的设备限速和完整目标发布确认仍然作用于裁剪后的目标。本策略没有复制脚本的 ROS 发布通道，
也不绕过 RC 的控制权、租约或生命周期处理。

夹爪阈值使用 `policy.yaml` 的 `params.gripper_threshold`，默认 `-0.8`：严格大于阈值输出
`0.04`，否则保留原值。支持热更新，保留模型和历史；配置提交换代时丢弃旧计划，从新观测重新规划。

## 使用

核对本目录自己的 `model.json` 和 `deployment.env`，使用 ACOne history checkpoint。
RC 控制频率统一配置为30Hz；修改 RC 频率需重启 RC。

`model.json` 的 `inference.generate_language` 可设为 `["Memory", "Scene", "Subtask"]`，设为 `[]` 关闭。
修改后用 `switch-model model.json` 冷重载。开启语言生成时前缀采用普通执行，去噪 CUDA Graph 仍可使用。
monitor 返回最近成功预测的 `subtask`；未启用、未生成、缺少该字段或 reset/release 后返回空字符串，预热文本不展示。

```text
policy switch qwen35_history_clip
policy qwen35_history_clip config set params.gripper_threshold -1.2
policy qwen35_history_clip config set params.arm_clip_enabled false
policy qwen35_history_clip config save
resume
```

切换成功后保持暂停，使用明确实例名操作。该策略按临时需求提供，按用户要求没有新增或运行测试；
本轮没有构建 Docker 镜像、加载真实模型或执行真机动作。

## TUI 模型参数与保存

所有模型 JSON 叶字段支持 `policy <实例> config set model_config.<section>.<field> <值>`。
`config show` 展示本实例内存快照，`config save` 写回当前模型 JSON 和策略 manifest；其他实例的内存配置不受影响。
参数生效方式、接近点融合和裁剪计数见[统一说明](../../docs/history-policy-configuration.md)。

臂阈值通过 `params.arm_clip_max_delta_rad` 热更新。policy 给新 chunk 附带逐动作、逐关节裁剪标记，RC 裁剪前缀时同步裁剪标记，只在确认消费后按实例/context 累计。
monitor 的 `arm_clip_count` 为已消费的裁剪动作条数，`arm_clip_joint_count` 为对应关节总次数；重复回调、重复确认和被丢弃前缀不重复计数。普通 reset 保留累计计数，重初始化/重载清零。
`arm_clip_enabled` 显示当前开关。关闭期间的新预测标记全为 false，之前已经接纳的目标仍保留原标记。
