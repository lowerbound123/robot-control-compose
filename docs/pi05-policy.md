# π0.5 / LeRobot 策略

`pi05` 加载 **LeRobot 格式的 ACOne 微调 checkpoint**，在独立的 Python 3.12 镜像中运行。策略通过 RC 读取双臂状态和三路 RGB 图像，返回 14 维关节目标；只有 RC 控制线程发送 ROS 指令。

实现以 LeRobot **0.6.1** 为兼容目标，运行依赖在 `policy/pi05/dependencies/requirements.lock` 固定。它与 Qwen 的 Transformers 实现、依赖和镜像相互独立。当前不是 OpenPI JAX checkpoint 加载器，也不自动转换仅含 LoRA/PEFT adapter 的目录。

## 准备完整 checkpoint

把训练导出的 `pretrained_model` 完整目录放到模型挂载内，包括：

```text
models/pi05/
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
    该 tokenizer 的其他配置文件
```

处理器状态文件名以两个 processor JSON 的 `state_file` 为准，不能只拷贝模型权重。策略直接使用 checkpoint 保存的归一化/反归一化数据，不读取 Qwen 的 `stats_key` 或 `stats.json`。旧 checkpoint 没有保存 processor 时，需要在对应训练环境导出完整前后处理器；不要用空统计或其他模型的统计替代。

tokenizer 必须是训练时使用的 PaliGemma tokenizer，提前准备到本地目录。也可以将 `model.tokenizer_path` 指向完整 HF 缓存中的实际 `snapshots/<revision>` 目录；缓存的 `blobs` 和符号链接目标必须在容器中可见。完整微调权重已经包含模型参数，无需额外挂载一份 PaliGemma 基础模型权重。启动时不会从网络下载 tokenizer 或权重。

当前要求 checkpoint 声明：

- `type: "pi05"`。
- `input_features["observation.state"]` 为 14 维。
- `output_features["action"]` 为 14 维；内部 `max_action_dim=32` 的 padding 由原生模型处理。
- 三个真实相机 feature，与配置的相机映射精确对应。
- `0 < n_action_steps <= chunk_size`。
- 未启用额外 LeRobot RTC、历史视觉/状态记忆或空相机。

训练版本未知时，会按上述固定版本尝试严格加载。未知配置字段、损坏权重、missing/unexpected keys 或不完整统计数据会报错，不会静默忽略。保存的 `normalize_observation_keys` 若漏掉需要归一化的状态或相机，也会在加载模型前报错，需重新导出一致的处理器。LeRobot 0.6.1 的 PI05 专用加载覆盖函数存在吞掉权重加载异常的路径，本项目使用其基类的严格 safetensors 加载路径，保留共享权重语义并传播错误。

## 配置与构建

在实机仓库根目录运行：

```bash
./setup.sh --config-only
```

编辑 `policy/pi05/deployment.env`，例如：

```dotenv
GPU_DEVICE=0
MODELS_DIR=/实际模型目录
HF_CACHE_DIR=/实际缓存目录
```

`MODELS_DIR` 映射为 `/models`，`HF_CACHE_DIR` 映射为 `/data/huggingface`。这些宿主目录需要提前存在；相对路径以策略目录为基准。`deployment.env` 用于 Compose 插值，不是需要执行的 shell 脚本。

编辑 `policy/pi05/model.json`：

- `model.checkpoint`：容器内完整微调 checkpoint 路径。
- `model.tokenizer_path`：容器内 tokenizer 目录。
- `runtime.instruction`：任务指令。
- `observations.camera_map`：左侧固定为 `head/left_wrist/right_wrist`，右侧必须填写 checkpoint `input_features` 的真实相机名称。模板中的 `observation.images.head` 等只是示例。
- `actions.representation`：根据实际训练约定填写 `absolute` 或 `delta_joint`，见下一节。
- `model.device`：默认 `cuda`；`cpu` 可用于离线加载/接口诊断，不代表具备实时控制性能。

准备后构建：

```bash
./setup.sh --policies pi05
```

不需要修改基础 Compose 或 RC 的策略列表。构建期间安装锁定依赖，`switch` 时只加载已有镜像和本地资产。

## 关节和夹爪单位

RC 状态/目标顺序固定为：

```text
[左臂6关节, 左夹爪, 右臂6关节, 右夹爪]
```

默认 `state_scale` 全 1、`state_offset` 全 0，表示微调数据已经使用同样的 ACOne 通道顺序和单位。不要仅凭维度相同就认为模型定义相同；需核对训练数据中左右臂、关节方向和夹爪开合值。

若训练使用不同但可通过逐维仿射映射表达的单位，配置 14 维 `observations.state_scale/state_offset`：

```text
模型坐标下的状态 = RC qpos × state_scale + state_offset
RC 绝对目标 = 模型坐标下的绝对目标减去 state_offset，再除以 state_scale
```

scale 必须非零且有限。这个变换发生在归一化前、反归一化后，不替代训练统计数据；也不会自动套用 ALOHA 的关节翻转或夹爪转换。

`actions.representation="absolute"` 表示处理器输出已经是模型坐标下的绝对目标。如果 checkpoint 保存了启用的 relative/absolute processor 对，加载时会恢复它们共享的观测状态；必须保留 `absolute`，防止二次叠加。

`delta_joint` 仅用于训练时输出关节增量、但保存的后处理器没有转换回绝对值的 checkpoint。对 12 个关节加上**本次观测**对应的模型坐标状态；两个夹爪通道仍按绝对值处理，随后做上述逆映射。它不是逐步累加增量。

π0.5 不使用 Qwen 专属的夹爪 `+0.15` 修正。

## chunk 裁剪与融合

π0.5 和两个 Qwen 策略共用 SDK 的 `AsyncChunkPolicy`：

1. 一次预测 checkpoint 的 `chunk_size` 步，取前 `n_action_steps` 步作为可执行 chunk。
2. `request_when_remaining=null` 时，阈值取可执行长度的 60% 向上取整；50 步对应剩余**少于 30 步**时提前预测下一批。也可显式填写有效阈值。
3. 新结果收集时，按发起以来实际消费的完整目标数裁去过时前缀。
4. 全部重叠部分使用与 Qwen35 相同的指数权重，参数为 `5.0` 和 `0.25`；重叠部分的夹爪保留旧计划。
5. 新轨迹替换剩余缓存，再由 RC 限速发送。推理始终最多一个在途任务。

配置声明为 sync 回调，每完整动作调用一次，但昂贵的模型推理在策略内部后台执行。图像先复制再交给后台线程。暂停会让旧结果失效；恢复时重置模型/处理器与缓存，不重新加载权重。推理超时或失败进入现有 policy 故障处理，不能通过旧代次结果恢复动作。

## 运行

可以先在不连接机器人、不启动 RC 的情况下检查 GPU 加载和预热。以下命令在仓库根目录执行，会读取本策略 `deployment.env` 的挂载和 GPU 设置：

```bash
docker compose --env-file policy/pi05/deployment.env -f policy/pi05/compose.yaml \
  run --rm --no-deps --user "$(id -u):$(id -g)" \
  -v "$PWD/policy/pi05:/workspace/policy/pi05:ro" --entrypoint python policy \
  -m rc_pi05_policy.probe --config /workspace/policy/pi05/model.json
```

此检查至少运行一次合成观测预热，成功时输出 `status: ready`、执行设备与 action 形状。它不发布 ROS 指令，也不代表真实相机内容、动作单位和任务效果已验证。检查结束会释放模型并删除临时诊断容器。

先按 [真机部署说明](ubuntu22-amd64-from-scratch.md) 启动 RC；机器人和相机驱动由现场独立管理。RC 中执行：

```text
switch pi05
status
```

等待 `policy pi05 ready；等待 resume` 后检查相机、关节反馈和实际训练单位，再使用 `resume`。`pause` 暂停，`exit` 执行统一退出清理。

更换另一份配置：

```text
policy pi05 switch-model model_alt.json
```

该命令先做本地文件/格式预检，再完整卸载旧实例、创建新实例。成功后仍暂停。模型与参数全部冷改，不支持在推理中热替换路径、统计数据或任务指令。

## 验证边界

测试包括共用调度与 Qwen 行为对照、模型配置预检、真实 LeRobot 保存/重载的 processor、严格权重错误、相对动作和单位转换，以及缩小网络规模后的原生 PI05 flow-matching 采样。缩小网络测试使用合成参数，仅用于验证加载和接口，不证明微调模型效果。

用户的实际 checkpoint、NVIDIA GPU、推理延迟和机器人动作需要在目标机器另行验证。镜像构建或 CPU fixture 通过不能替代这些验收。

## 官方来源

- [LeRobot π0.5 文档](https://huggingface.co/docs/lerobot/pi05)
- [LeRobot v0.6.1](https://github.com/huggingface/lerobot/releases/tag/v0.6.1)
- [固定版本的模型类](https://github.com/huggingface/lerobot/blob/7e241bd630a3719a56157a497ce5d08f244784f1/src/lerobot/policies/pi05/modeling_pi05.py)
- [原生 processor 工厂](https://github.com/huggingface/lerobot/blob/7e241bd630a3719a56157a497ce5d08f244784f1/src/lerobot/policies/factory.py)
- [Physical Intelligence π0.5 研究](https://www.pi.website/blog/pi05)
