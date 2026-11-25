# a2_optimized_lift2

LIFT2 的 A2 视觉历史策略。当前三路相机、头相机历史和关节状态生成 50 步动作。每完成四个新动作块，策略使用该轮视觉特征在后台刷新 Memory、Scene、Subtask。每个实例独立持有模型、视觉历史、语言缓存和动作队列。

状态归一化与动作反归一化均读取 checkpoint 的 `stats.json["arx_lift2"]`。加载、切换模型和配置修改时都校验这组统计量。输出按 LIFT2 的 14 维关节顺序还原。左右夹爪在第 6、13 列（从 0 计），新预测值大于等于 2.5 时默认输出 4.9，小于 2.5 时默认输出 0。可用本实例的四个 `params.gripper_*` 设置调整阈值、赋值或增量。

## 部署与使用

```sh
./setup.sh --config-only
# 在 policy/a2_optimized_lift2/ 下配置 deployment.env 和 model.json
./setup.sh --policies a2_optimized_lift2
```

在已配置 LIFT2 connector 的 RC 中选择策略。模板采用同步调度，以下命令选择异步运行并启用动作融合。

```text
policy switch a2_optimized_lift2
policy a2_optimized_lift2 config set execution.mode async
policy a2_optimized_lift2 config set model_config.execution.pipelined true
policy a2_optimized_lift2 config set model_config.transition.trunk_mode tail
policy a2_optimized_lift2 config save
monitor a2_optimized_lift2
resume
```

该来源独立配置、保存和运行，与 ACOne A2 共用只读模型源码和依赖镜像。两者使用相同的单次 checkpoint 加载、FLA、去噪 CUDA Graph、等长视觉注意力合批、历史帧批量转换和 eager 前缀缓存。异步融合支持 `close` 或 `tail`，RC 在回包安装时从首个可替换目标重新匹配。动作与语言刷新分段计时见 [A2 Monitor](../a2_optimized/README.md#monitor)。
