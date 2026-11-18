# π0.5 / LIFT2

`pi05_lift2` 加载 `qwen3_5_dev` 已验证的 **Pi05 训练导出格式**（例如 `pi05_zero`），使用独立的 Python 3.12 / Torch 2.7.1+cu128 / Transformers 4.57.1 镜像和配套模型补丁。模型运行时、输入处理、统计读取均位于本策略目录，不导入训练仓库、Conda 或 ROS。原 `pi05` 继续支持 LeRobot 0.6.1 的原生 saved processors，两者的镜像不再共用。

## 模型与配置

```text
models/pi05_zero/
  config.json
  train_config.json
  stats.json
  model.safetensors
models/paligemma-3b-pt-224/
  tokenizer_config.json
  tokenizer.json 或 tokenizer.model
  special_tokens_map.json 等该 tokenizer 的配套文件
```

`pi05_zero` 包含完整视觉、语言及动作权重。直接构造相同架构并严格加载全部参数，不需要重复加载 PaliGemma 基础权重。缺失、额外、形状错误或冲突的 tied embedding 会报错，不会使用随机权重继续运行。tokenizer 必须是训练使用的 PaliGemma tokenizer；全部文件离线加载，不会从 Hub 自动下载。

`train_config.json` 用于验证变换顺序，`stats.json` 固定选 **arx_lift2**。当前支持该 dev 导出的 `mean_std → compose_fields → pi05_gemma_tokenizer → pad_state_and_action` 输入链，12 关节 delta、两夹爪绝对值；不支持 16 维空位重排或其他训练变换。config.json 内的 QUANTILES 声明不代表实际训练变换，本策略以 train_config.json 为准并明确验证。

初始化模板后，编辑本目录的 `model.json` 与 `deployment.env`：

```bash
./setup.sh --config-only
```

- `model.checkpoint`：容器内 checkpoint 目录，默认 `/models/pi05_zero`。
- `model.tokenizer_path`：容器内 tokenizer 目录，默认 `/tokenizer`。
- `runtime.instruction`：本次任务指令。
- `observations.camera_map`：固定 head→image0、left_wrist→image1、right_wrist→image2（完整键见模板）。RGB HWC 转 CHW、0..1，按比例 bilinear resize 并居中零填充至 224×224；模型内部再映射至 -1..1。
- `observations.state_scale/state_offset`：默认全 1/全 0。按 `qpos × scale + offset` 转成模型单位。
- `actions.representation`：必须为 `delta_joint`，与上述训练链一致。

状态使用 `(state - mean) / (std + 1e-6)`，连续 14 维尾部补零到 32 维，除以 3 后离散化；不重排、不额外裁剪。提示格式保持 `Task: ..., State: ...;\nAction: `，最长 200 tokens，保留 tokenizer 自身 BOS 等设置。

模型返回 `[1, horizon, 32]`。取连续前 14 列，以 `raw × (std + 1e-6) + mean` 反归一化，给 12 个关节加回本次观测的模型单位 qpos；第 6、13 列夹爪不加锚点。最终按 `(目标 - offset) / scale` 还原 RC 单位，取 `n_action_steps` 前缀。任何非有限结果都拒绝提交。

## 从旧 pi05_lift2 配置升级

这次需要重建 **pi05_lift2 独立镜像**，不能继续复用原 pi05 镜像。旧 `model.json` 里的 `preprocessor_filename`、`postprocessor_filename` 已不适用；setup 合并会保留用户旧值，不能代替下面的语义更新：

1. 保留自己的任务指令、路径、单位映射和执行参数，按 `model.json.example` 删除两个 processor 文件名字段。
2. 将 camera_map 改为 image0/image1/image2，将 representation 改为 `delta_joint`。
3. checkpoint 指向 `pi05_zero`；tokenizer_path 指向 `/tokenizer`，不能仍指向仅有模型权重的 `pi05_zero`。
4. 在 `deployment.env` 加入 `TOKENIZER_DIR`，指向主机已有 PaliGemma 目录。该目录只读挂载；不用复制其中的大模型文件。

服务器上已核实的路径可配置为：

```text
MODELS_DIR=/home/pjlab/zhouyuxiang/robot-control-compose/models
TOKENIZER_DIR=/home/pjlab/hongrui_workspace/paligemma-3b-pt-224
GPU_DEVICE=0
```

这是部署示例，本轮代码修改不自动改写服务器配置或加载策略。

## 运行时切换任务

`params.task` 为任务覆盖值，默认 `null`，使用 `model.json` 的 `runtime.instruction`。切换命令：

```text
policy pi05_lift2 switch-task "Pick up the orange cup and place it in the tray."
policy pi05_lift2 config save
```

也可通过 `policy pi05_lift2 config set params.task "新的任务"` 设置，输入 `null` 恢复模型配置中的默认指令。空文本和非字符串被拒绝。保存写回当前实例对应的 `policy.yaml`，重新加载后继续使用该任务；不改写 `model.json` 的默认指令。

切换保留已加载模型和执行进程。先收齐并丢弃旧任务的在途结果（包括旧任务错误），清除旧动作计划及 processor 临时状态，再用新任务和新观测预测；旧动作不会混入新任务的融合。已经提交给设备的动作仍按原 FIFO/发布确认规则收尾。切换需要等待在途推理安全结束，不承诺立即取消模型计算；暂停状态不会因切换任务自动恢复运动。

更新源码后先运行 `./setup.sh --config-only`，补齐旧 `policy.yaml` 的 `params.task`、`switch-task` 和 task/夹爪热更新白名单；已有参数值保留。随后重新加载对应策略实例一次。此功能不增加依赖，无需重建镜像。

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

## 推理监控

在 TUI 输入 `monitor pi05_lift2` 查看当前任务、后台预测、动作余量、裁剪和阶段耗时。再次输入可关闭面板。[π0.5 监控字段说明](../../docs/policy-monitor.md#π05-策略)

## 镜像与启动

```bash
./scripts/build.sh pi05_lift2
```

镜像为 `robot-control-compose-pi05-lift2:local`，构建会核对并安装五个固定版本的 Transformers 补丁。加载时再次检查版本和补丁摘要；使用旧镜像会明确报错。其来源及推理数学差异见 [THIRD_PARTY.md](THIRD_PARTY.md)。

connector 独立配置；选择本策略不会自动切换设备或启动厂商驱动。按现有方式启动 RC 后：

```text
policy switch pi05_lift2
status
```

加载完成保持暂停，再按现场流程使用 `resume`。更换模型配置仍使用 `policy pi05_lift2 switch-model model_alt.json`。替换源码后需重新加载对应策略进程。

独立 GPU 加载/预热检查不连接机器人：

```bash
RC_HOST_PROJECT_ROOT="$PWD" docker compose --env-file policy/pi05_lift2/deployment.env -f policy/pi05_lift2/compose.yaml \
  run --rm --no-deps --user "$(id -u):$(id -g)" \
  -v "$PWD/policy/pi05_lift2:/workspace/policy/pi05_lift2:ro" --entrypoint python policy \
  -m rc_pi05_lift2.probe --config /workspace/policy/pi05_lift2/model.json
```

定向测试使用模型元数据、小尺寸真实算子和受控推理输出，覆盖严格权重加载、图像/token/动作路径、夹爪和缓存、热更新、sync/async worker 及 example 自链。这些检查不代表完整 checkpoint GPU 延迟或真机任务效果已经验收。
