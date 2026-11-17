# π0.5 / LIFT2

`pi05_lift2` 是独立的策略来源，加载 **LeRobot 格式的 LIFT2 微调 checkpoint**，返回 RC 的 14 维关节目标。它拥有自己的入口、`src/rc_pi05_lift2/`、配置和依赖锁文件，复用现有 `robot-control-compose-pi05:local` Python 3.12 依赖镜像；每个运行实例仍使用独立容器、host、执行进程和 context。原 `pi05` 的实现和输出行为保持不变。

模型接口与现有 pi05 相同，固定兼容 LeRobot 0.6.1。归一化和反归一化使用该 checkpoint 保存的 processor 及统计数据；本策略不读取 Qwen 的 `stats.json` 或 `arx_lift2` 统计键。模型必须按 LIFT2 的通道顺序、单位与相机语义训练。仅有 14 维形状不能证明训练单位正确。

## 模型与配置

准备完整本地 checkpoint：

```text
models/pi05_lift2/
  pretrained_model/
    config.json
    model.safetensors
    policy_preprocessor.json
    policy_preprocessor_step_*.safetensors
    policy_postprocessor.json
    policy_postprocessor_step_*.safetensors
  tokenizer/
    tokenizer_config.json
    tokenizer.json 或 tokenizer.model
```

processor 状态文件名以 JSON 中的 `state_file` 为准，不能缺失或借用其他模型的统计数据。tokenizer 使用训练时的本地 PaliGemma tokenizer。加载保持离线和严格检查，不自动转换 OpenPI JAX 或仅含 LoRA adapter 的目录。

初始化本地配置模板：

```bash
./setup.sh --config-only
```

编辑 `policy/pi05_lift2/deployment.env` 中的 `MODELS_DIR`、`HF_CACHE_DIR`、`GPU_DEVICE`，以及本目录 `model.json`：

- `model.checkpoint`：容器内 LIFT2 checkpoint 目录，模板为 `/models/pi05_lift2/pretrained_model`。
- `model.tokenizer_path`：训练所用 tokenizer 的本地目录。
- `runtime.instruction`：任务指令。
- `observations.camera_map`：将 RC 的 `head/left_wrist/right_wrist` 映射到 checkpoint 中三个真实相机 feature。
- `observations.state_scale/state_offset`：14 维显式单位映射，默认全 1/全 0。
- `actions.representation`：`absolute` 或 `delta_joint`，必须与训练及保存的 processor 一致。

状态和动作顺序为 `[左臂6关节, 左夹爪, 右臂6关节, 右夹爪]`。模型状态使用 `RC qpos × scale + offset`，反归一化后的绝对目标按 `(模型目标 - offset) / scale` 还原。`delta_joint` 仅给 12 个关节加回本次观测，夹爪保持绝对值；如果保存的 absolute processor 已完成转换，必须使用 `absolute`。完整的模型与 processor 契约见 [pi05 说明](../pi05/README.md)。

## 夹爪规则

输出副本的第 6、13 列（从 0 开始）使用与 LIFT2 history 相同的四参数规则：

| 参数 | 含义 | 默认值 |
| --- | --- | --- |
| `params.gripper_upper_threshold` | x1，上限 | 2.5 |
| `params.gripper_upper_value` | y1，上限输出 | 4.9 |
| `params.gripper_lower_threshold` | x2，下限；null 禁用下限 | 2.5 |
| `params.gripper_lower_value` | y2，下限输出 | 0.0 |

先判断新预测的夹爪值 `g >= x1`，否则判断启用下限且 `g <= x2`。命中的 y 是数字时赋值，是有正负号的字符串时计算 `g + float(y)`；均未命中保留 g。数字 `-0.5` 是赋值，字符串 `"-0.5"` 是减 0.5。两条规则不对结果再次判断，重叠时上限优先。因此默认新预测恰好等于 2.5 时处理为 4.9。

每次提交后台推理时快照规则，在反归一化、单位还原后对新预测副本处理一次，再进入 SDK 的裁剪与融合。canonical 保存已处理、已融合动作；重叠区间保留旧夹爪计划，不再次阈值化或加 delta。原始模型数组不被修改，缓存直返不重复处理；完成时间包含夹爪处理，仍受原推理期限约束。不使用 Qwen 的 `+0.15` 夹爪修正。

```text
policy pi05_lift2 config set params.gripper_upper_threshold 3.0
policy pi05_lift2 config set params.gripper_upper_value 4.9
policy pi05_lift2 config set params.gripper_lower_threshold 2.0
policy pi05_lift2 config set params.gripper_lower_value 0.0
policy pi05_lift2 config save
```

将 y 改为 `"+0.5"` / `"-0.5"` 可配置增量，例如 `policy pi05_lift2 config set params.gripper_upper_value "+0.5"`。保存和 setup 合并保留字符串类型；有限性、溢出检查及完整示例见[统一夹爪说明](../../docs/history-policy-configuration.md#夹爪输出规则)。数字赋值也已移动到融合之前，启用融合时的输出可能不同于旧版。

夹爪参数支持普通热更新并保存到本目录 `policy.yaml`，不卸载模型权重。RC 配置换代后，本策略先排空旧后台预测、清除旧计划并重置 processor 的临时状态，再以新观测规划；已提交动作按原 FIFO 规则收尾。仅改变 RC 的 `execution.mode/call_every_actions` 时保留当前动作计划。重复实例使用精确实例名。

已有配置若仍使用旧夹爪字段，先按[参数改名升级说明](../../docs/history-policy-configuration.md#夹爪参数改名升级)迁移；`setup.sh --config-only` 也会在合并默认值前完成改名并备份。运行时只接受上述四个统一名称。

## 调度与生命周期

沿用 pi05 的 SDK `AsyncChunkPolicy`：预测完整模型 horizon，取 `n_action_steps` 可执行前缀；默认余量阈值是可执行长度的 60% 向上取整，低于阈值时后台预测下一批。只允许一个在途模型任务，按已消费目标裁剪过时前缀，重叠区间保留旧夹爪计划。

后台推理与外层回调可重叠。RC 的 sync/async 决定回调等待期间发送 hold 还是后续缓存，策略内部的预测触发保持 pi05 的既有规则。后台只持有复制后的状态和图像；暂停恢复、reset、重载及退出仍使用 SDK/RC 的租约和生命周期确认。未通过模型、配置或处理器校验时不会生成可发送动作。

## 镜像与启动

已有 pi05 镜像时可直接复用。新策略的 Dockerfile 和依赖构建输入与 pi05 相同，不需因新增来源重复构建。没有该镜像时运行：

```bash
./scripts/build.sh pi05_lift2
```

connector 独立配置；选择本策略不会自动切换设备或启动厂商驱动。按现有部署方式启动 RC 后：

```text
policy switch pi05_lift2
status
```

加载完成保持暂停，再按现场流程使用 `resume`。更换模型配置使用：

```text
policy pi05_lift2 switch-model model_alt.json
```

模型参数和任务文本仍按 pi05 的冷配置方式管理。替换源码需重新加载对应策略进程；不要用在原模块上重新初始化的操作代替源码重载。

独立 GPU 加载/预热检查可以使用本目录的 `rc_pi05_lift2.probe`，不连接机器人：

```bash
RC_HOST_PROJECT_ROOT="$PWD" docker compose --env-file policy/pi05_lift2/deployment.env -f policy/pi05_lift2/compose.yaml \
  run --rm --no-deps --user "$(id -u):$(id -g)" \
  -v "$PWD/policy/pi05_lift2:/workspace/policy/pi05_lift2:ro" --entrypoint python policy \
  -m rc_pi05_lift2.probe --config /workspace/policy/pi05_lift2/model.json
```

本地定向测试覆盖发现/配置、夹爪、缓存与热更新、真实 worker 图像释放和 example 自链组合，以及原生 LeRobot CPU processor 的归一化和坐标转换。模型前向使用合成替身；这些检查不代表实际 checkpoint、GPU 延迟或真机任务效果已经验收。
