# qwen35_history_clip

从 `qwen35_history` 复制的临时 ACOne 策略。入口、`src/rc_qwen35_history_clip/`、
`vendor/`、构建输入和配置全部归本目录，不从另一个 policy 导入业务代码。
每个运行实例独占容器、host 和执行进程，复用已有的 Qwen history 依赖镜像。

## 动作裁剪

沿用 ACOne 的 14 维双臂布局、归一化和历史推理，在输出前额外应用 `infer_a2.py`
的臂关节裁剪：每个臂关节相对上一条裁剪指令最多变化 `±0.1 rad`，两个夹爪不参与裁剪。
首条指令以本次 context 首轮观测的 `qpos` 为锚点，后续以已消费的裁剪指令为锚点。
跨 chunk 保留这个锚点；没有消费动作的重复回调不会把锚点向前推进，reset 后重新使用反馈位置。

每条目标被裁剪后的值替代；不会追加动作去补齐原始期望终点，因此最终位置可能与原预测不同。
RC 的设备限速和完整目标发布确认仍然作用于裁剪后的目标。本策略没有复制脚本的 ROS 发布通道，
也不绕过 RC 的控制权、租约或生命周期处理。

夹爪阈值使用 `policy.yaml` 的 `params.gripper_threshold`，默认 `-0.8`：严格大于阈值输出
`0.04`，否则保留原值。支持热更新，保留模型和历史；配置提交换代时丢弃旧计划，从新观测重新规划。

## 使用

核对本目录自己的 `model.json` 和 `deployment.env`，使用 ACOne history checkpoint。
RC 控制频率统一配置为30Hz；修改 RC 频率需重启 RC。

```text
policy switch qwen35_history_clip
policy qwen35_history_clip config set params.gripper_threshold -1.2
policy qwen35_history_clip config save
resume
```

切换成功后保持暂停，使用明确实例名操作。该策略按临时需求提供，按用户要求没有新增或运行测试；
本轮没有构建 Docker 镜像、加载真实模型或执行真机动作。
