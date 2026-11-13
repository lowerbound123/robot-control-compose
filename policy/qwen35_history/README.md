# Qwen3.5 视觉历史策略

`qwen35_history` 将 `infer_a2.py` 的三路当前视觉、头相机历史视频和 Flow Matching 动作专家接入现有 policy 接口。它使用独立 Python 3.11 容器、模型、context 和推理源码；不依赖训练仓库，不导入 ROS，不修改现有 `qwen35_single/twostage`。

业务实现位于本目录 `src/rc_qwen35_history/`，模型源码及来源记录在 `vendor/`；所有专用依赖和构建文件也在本目录。

## 准备与配置

```bash
./setup.sh --config-only
# 编辑 policy/qwen35_history/deployment.env 和 model.json，准备模型与缓存
./setup.sh --policies qwen35_history
```

`deployment.env` 指定 GPU_DEVICE、MODELS_DIR、HF_CACHE_DIR，默认 HF_HUB_OFFLINE=1。checkpoint 必须包含 `config.json`、`train_config.json`、`stats.json`、`model.safetensors`，模型类型必须是 `qwen3_5_context_video_ki`。当前来源固定预测 50 步，状态/动作宽度为 32，适配机器人为 arx_acone 的 14 维双臂关节布局。

`model.vlm_model_name_or_path` 默认 `Qwen/Qwen3.5-4B`，也可以填写容器内本地路径。模型和 processor 使用同一个地址，覆盖训练配置中原训练机的绝对路径。所有资产须提前准备在模型目录或缓存中；启动不安装依赖或下载模型。相对 checkpoint/本地基础模型路径以 model.json 所在目录解析。

```text
policy switch qwen35_history
resume
pause
policy qwen35_history switch-task "新的任务"
policy qwen35_history switch-model model_alt.json
resume
```

任务覆盖值在 manifest 的 `params.task` 中；`null` 恢复 model.json 的 `runtime.instruction`。可使用 `policy qwen35_history config set params.task "新任务"`，重复实例使用精确实例名。配置入口的 task 修改请求 reset：等待旧后台推理退出、清空历史和动作计划，保留模型并暂停，随后显式 resume。注册的 switch-task 热更新仍保留原运行状态。冷重载成功后暂停。MCP 命令继续遵循 take/return 和实例代次检查；本策略不自动接管、复位或恢复运动。

## X、回调模式与模型流水线

RC 的 `execution.mode` 可选 `sync` / `async`，`call_every_actions` 是 RC 发起策略调用的间隔，策略不限定这两个值；`control_mode` 仍需 `qpos`。是否启动模型推理由策略检查输入剩余动作数量决定，`model.json` 的 `execution.pipelined` 只控制新旧动作是否融合。

| model.json 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `execution.replan_every_actions` | 25 | X；剩余动作 <= 50-X 时推理，否则原样返回，范围 1～50 |
| `execution.pipelined` | false | false 返回完整新预测，true 与输入剩余 chunk 融合后返回 |
| `execution.inference_timeout_s` | 60 | 从提交时开始计时的单次推理期限 |
| `inference.num_inference_steps` | null | 使用 checkpoint 的 Euler 步数，当前为 10 |
| `inference.use_cuda_graph` | true | 尝试捕获专家去噪循环 |
| `inference.use_prefix_graph` | true | 去噪图可用时，尝试捕获各视觉布局的前缀 |
| `inference.generate_language` | 示例为 `["Memory", "Scene", "Subtask"]` | 生成所选语言字段；`[]` 关闭，旧配置缺少该字段时保持关闭 |
| `history.enabled` | true | 使用进程内头相机历史 |

上述字段均可通过 TUI 的 `model_config.<section>.<field>` 设置；执行/历史参数重置上下文并保留权重，模型/推理参数重新初始化。外部编辑文件后用 `switch-model` 显式重载。policy.yaml 的 `params.task`、`params.gripper_threshold` 支持热改。

夹爪阈值配置为 `params.gripper_threshold`，默认 `-0.8`，只接受有限数值。用
`policy qwen35_history config set params.gripper_threshold -1.2` 热更新，
`policy qwen35_history config save` 保存到本来源配置；重复实例使用精确实例名。
修改阈值不重载模型、不清空历史。RC 配置提交推进代次后，策略丢弃旧代次的剩余计划和在途预测，
从新观测重新规划并应用新阈值；已经提交给 connector 的动作不撤回。

每次策略调用先检查 RC 传入的剩余 chunk。剩余数量大于 `50-X` 时直接原样返回，不推理、不重复做夹爪或硬裁剪处理；否则完成一次模型推理。`pipelined=false` 返回完整 50 条新预测，`true` 使用 SDK 接近点拼接融合输入旧 chunk 和新预测。默认旧段尾点匹配新段最近点，全部 14 维包含夹爪参与融合。

一次需要推理的回调会等本轮模型完成。RC 为 async 时继续发送旧缓存，为 sync 时等待回调返回。回包始终保持请求时的动作起点，只有 RC 裁掉请求到回包期间实际消费的前缀，并保留已开始/已提交目标。融合缓存按实际长度消费；模型预测和待执行队列不再在 policy 内重复维护两条时间线。

历史通过独立 observe 回调按消费计数采样，模型推理等待时也可更新。推理使用请求消费时刻的历史快照，迟到的请求不会混入未来观测。观察回调漏步时仍以最新图补齐并告警。超时、异常和非有限预测继续进入既有故障处理。

## 历史、动作与模型

历史只属于本实例 context，不写文件、不放入共享全局状态。每个已消费动作保存一张经过来源缩放和量化的头相机图：默认 CHW uint8、长边192；最多保存481张。第n个动作完成后的观测记为索引n−1，历史时钟为n。当前观测三路图默认长边256。

推理仍按 checkpoint 的 `context_slot_count=32`、`context_slot_stride=15` 采样，即 t−480 到 t−15，旧帧在前。X改变的是推理时刻及该时刻可见的历史，不改变15步采样间隔。不足的历史不补齐到32张；重复消费计数、暂停、等待推理和限速中间周期不增长历史。观察漏步会补帧并告警，消费计数或代次倒退明确报错。

共享图像在回调内复制，缩放后的历史拥有独立内存。后台推理仅持有自有当前图像与不可变历史快照；不保留共享 mmap、切片或旧 runtime。reset/release 清空历史，重复来源实例互相隔离。

前缀每轮重新编码当前图像、任务、状态和历史视频，获得 KV；动作专家复用这份 KV 完成10次 Euler 更新，输出 `[1,50,32]`。按 schema 逆重排到14维后反归一化，delta仅加回12个臂关节的本轮观测位置，夹爪保持绝对值。融合后夹爪严格大于 `params.gripper_threshold`（默认−0.8）时置为0.04，否则保留原值。训练 pad_keys 指定的动作维度固定为来源 home 值；不是运行时自动复位。

另有正式来源 [`qwen35_history_lift2`](../qwen35_history_lift2/README.md)：独立配置和执行实例，各自持有 history 实现与配置，并使用同一依赖镜像，固定使用所选 checkpoint 的 `stats.json["arx_lift2"]` 分别做状态归一化、动作反归一化，并按 LIFT2 schema 重排和还原。输出夹爪严格大于本实例配置阈值（默认 2.5）时输出 4.9、否则输出 0；该夹爪步骤保留手臂值和原始预测缓存。缺少 LIFT2 统计量会在初始化或模型切换预检时报错。本来源仍使用 ACOne 统计量与原夹爪规则；两者都要求 history 模型格式。

临时来源 [`qwen35_history_clip`](../qwen35_history_clip/README.md) 另行复制了完整实现，额外将臂关节目标按上一条已消费输出逐关节裁剪至 ±0.1 rad，允许最终目标改变。原 ACOne、LIFT2 history 不启用这层裁剪。

VLM及专家主体为BF16，专家小投影保持FP32。初始化使用合成480×640观测经过真实processor预热，既不读取机器人，也不增加历史。CUDA Graph 与 eager 使用相同噪声对照，归一化动作最大差值需不超过0.05；不支持的布局或捕获失败回退eager。CPU测试不能证明CUDA捕获可用。

`inference.generate_language` 只接受 `Memory`、`Scene`、`Subtask` 的无重复列表，按此顺序生成。
语言段生成结束后重新编码进动作前缀，动作专家使用包含它的 KV cache；这会影响动作条件，不只是额外显示文本。
开启任一语言字段时自动跳过前缀 CUDA Graph，去噪 CUDA Graph 仍可使用。
三个字段每轮重新生成，不自动把上一轮 Subtask 回填为 Memory。
修改本目录的 model.json 后，用 `policy <实例> switch-model model.json` 冷重载，完成后显式 resume。

不提供人类演示、FAST动作解码、IBQ未来图生成或历史拼图落盘。逐轮详情走 TRACE。

## 策略监控

两个 history 来源分别提供同一套执行指标。在策略加载成功后，用当前实例名打开面板：

```text
monitor qwen35_history
monitor qwen35_history_lift2
```

同一命令再次执行会关闭面板；链中重复来源使用对应实例名，例如 `qwen35_history-0`。监控只读取加锁的标量快照，不调用模型、Torch/CUDA、网络或动作接口，也不采集显存、内存、CPU 等资源信息。

| 类别 | 指标 |
| --- | --- |
| 模型与任务 | `robot_type`、`checkpoint`、`task`、`normalization`、`action_mode`、`model_dtype`、`num_inference_steps`、`max_prompt_tokens` |
| 执行状态 | `context_state`、`model_stage`、`model_stage_age_ms`、`context_uptime_s`、`pipelined`、`replan_every_actions`、`inference_timeout_s` |
| 推理计数 | `inference_count`（已提交）、`inference_completed`（模型成功返回）、`inference_errors`、`inference_busy`、`reset_count` |
| 请求等待 | `pending`、`pending_age_ms`、`pending_remaining_ms`、`last_submission_actions`、`last_prediction_age_s` |
| 语言生成 | `generate_language`（逗号分隔的字段）、`subtask`（最近成功预测的 Subtask）、`prefix_graph_disabled_reason` |
| 耗时 | `load_ms`、`warmup_ms`、`processor_ms`、`prefix_ms`、`denoise_ms`、`post_ms`、`predict_ms`、`predict_avg_ms`、`predict_max_ms` |
| 历史与图像 | `history_enabled`、`history_tick`、`history_frames`、`history_capacity_frames`、`history_slot_count`、`history_slot_stride`、`context_slots`，以及三路 `*_image_shape` 和图像缩放配置 |
| 动作缓存 | `chunk_size`、`cached_actions`、`consumed_actions`、`control_epoch`、`last_trim`、`trimmed_actions_total` |
| 加速路径 | `denoise_graph_requested`、`prefix_graph_requested`、`denoise_graph_ready`、`prefix_graph_layouts`、`prediction_path`、`prefix_tokens`、`max_prefix_tokens`、`graph_error` |
| 故障 | `last_error`；后台监控读取自身出错时显示 `backend_monitor_error`，不影响推理结果 |

`context_state=ready` 表示策略 context 已就绪，不代表 RC 已恢复发送；RC 的暂停/控制权仍看主状态栏。`model_stage` 显示预处理、prefix、去噪、后处理等当前阶段，空闲时为 `idle`。`inference_busy=0` 且 `pending=1` 表示请求仍待 RC 回调收取，不能仅凭 pending 判断模型仍在计算。

耗时为最近一次成功预测的墙钟耗时；平均/最大耗时、推理次数和裁剪累计值覆盖当前 context 生命周期，普通 reset 不清零，冷重载或重新初始化 context 后重新统计。历史与动作缓存指标随 reset 清零。毫秒字段以 `_ms` 结尾，秒字段以 `_s` 结尾；时间年龄为 `-1` 表示尚无成功预测。任务和错误文本最多显示 512 个字符。

初始化完成前 SDK 尚未创建 monitor，初始化失败应查看日志；日志会补充失败的模型阶段。监控不触发初始化、重载或恢复运动。

未启用 Subtask、尚无成功预测、当前生成段缺少 Subtask，以及 reset/release 后，`subtask` 均返回空字符串。
预热产生的文本不作为当前任务展示；monitor 只读取缓存，不触发语言解码。

## 验证与部署边界

```bash
python3 scripts/test_scope.py --policy qwen35_history
```

包含example基线、新策略调度/配置、真实本地host/worker及共享内存、CPU张量与部署静态检查。CPU模型测试需要Torch2.10、torchvision0.25、Transformers5.2；缺少时明确跳过。CUDA Graph回放测试需NVIDIA GPU。各层验证应独立报告，跳过不计通过。

ACOne 和 LIFT2 的 RC 控制频率统一为30Hz，切换 connector 时仍使用同一个 RC 频率。原模型以30动作步/秒编码视频时间；实际发送还会受RC限速与逐步回调开销影响，消费步数不等于控制周期或墙钟时间。修改已有部署的 RC 频率后需重启 RC。动作发送、设备限速、复位、控制权和清理仍由RC负责；未做真实checkpoint、GPU或硬件验收。

## TUI 模型参数与保存

所有模型 JSON 叶字段支持 `policy <实例> config set model_config.<section>.<field> <值>`。
`config show` 展示本实例内存快照，`config save` 写回当前模型 JSON 和策略 manifest；其他实例的内存配置不受影响。
参数生效方式、接近点融合和裁剪计数见[统一说明](../../docs/history-policy-configuration.md)。
