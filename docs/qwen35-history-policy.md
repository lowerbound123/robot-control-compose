# Qwen3.5 视觉历史策略

`qwen35_history` 将 `infer_a2.py` 的三路当前视觉、头相机历史视频和 Flow Matching 动作专家接入现有 policy 接口。它使用独立 Python 3.11 容器、模型、context 和推理源码；不依赖训练仓库，不导入 ROS，不修改现有 `qwen35_single/twostage`。

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

manifest 的 `execution.mode=sync`、`call_every_actions=1`、`control_mode=qpos` 必须保持不变：每个完整动作目标成功发送并消费后，RC 进入一次轻量回调。模型流水线由 **model.json** 的 `execution.pipelined` 决定，不通过更改 manifest 的 mode 开启。

| model.json 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `execution.replan_every_actions` | 25 | X；整数，范围 1～50 |
| `execution.pipelined` | false | false 同步，true 后台异步推理 |
| `execution.inference_timeout_s` | 60 | 从提交时开始计时的单次推理期限 |
| `inference.num_inference_steps` | null | 使用 checkpoint 的 Euler 步数，当前为 10 |
| `inference.use_cuda_graph` | true | 尝试捕获专家去噪循环 |
| `inference.use_prefix_graph` | true | 去噪图可用时，尝试捕获各视觉布局的前缀 |
| `history.enabled` | true | 使用进程内头相机历史 |

上述字段均为冷配置，编辑文件后通过 `switch-model` 重载实例；只有 `params.task` 热改。

同步模式预测完整 50 步，只返回前 X 步。消费完 X 步后用新观测推理，等待期间不继续消费旧 chunk。

异步模式首轮等待完整预测；此后每累计消费 X 步提交新推理，旧 chunk 后半段可以继续执行。结果按观测之后实际消费的步数裁掉过时前缀，再复用 SDK 的指数融合：整个重叠区间、factor=5、smoothing=0.25，重叠区保留旧夹爪计划。没有原脚本的 replan_ahead/blend_steps 两套阈值。

同时最多一个在途请求。推理跨过多个触发点后只提交一次当前请求，不补发；队列耗尽则等待当前请求。超时、异常、非有限动作或全过时结果进入现有 policy 故障处理，不自动清除故障或冷重试。

## 历史、动作与模型

历史只属于本实例 context，不写文件、不放入共享全局状态。每个已消费动作保存一张经过来源缩放和量化的头相机图：默认 CHW uint8、长边192；最多保存481张。第n个动作完成后的观测记为索引n−1，历史时钟为n。当前观测三路图默认长边256。

推理仍按 checkpoint 的 `context_slot_count=32`、`context_slot_stride=15` 采样，即 t−480 到 t−15，旧帧在前。X改变的是推理时刻及该时刻可见的历史，不改变15步采样间隔。不足的历史不补齐到32张；重复消费计数、暂停、等待推理和限速中间周期不增长历史。回调漏步、计数倒退或未重置的代次变化明确报错。

共享图像在回调内复制，缩放后的历史拥有独立内存。后台推理仅持有自有当前图像与不可变历史快照；不保留共享 mmap、切片或旧 runtime。reset/release 清空历史，重复来源实例互相隔离。

前缀每轮重新编码当前图像、任务、状态和历史视频，获得 KV；动作专家复用这份 KV 完成10次 Euler 更新，输出 `[1,50,32]`。按 schema 逆重排到14维后反归一化，delta仅加回12个臂关节的本轮观测位置，夹爪保持绝对值。融合后夹爪大于−0.8时置为0.04。训练 pad_keys 指定的动作维度固定为来源 home 值；不是运行时自动复位。

VLM及专家主体为BF16，专家小投影保持FP32。初始化使用合成480×640观测经过真实processor预热，既不读取机器人，也不增加历史。CUDA Graph 与 eager 使用相同噪声对照，归一化动作最大差值需不超过0.05；不支持的布局或捕获失败回退eager。CPU测试不能证明CUDA捕获可用。

首版不提供人类演示、语言段生成、FAST动作解码、IBQ未来图生成或历史拼图落盘。monitor可查看历史时钟、帧数、使用槽数、累计推理次数、在途状态、动作裁剪及阶段毫秒耗时；逐轮详情走TRACE。

## 验证与部署边界

```bash
python3 scripts/test_scope.py --policy qwen35_history
```

包含example基线、新策略调度/配置、真实本地host/worker及共享内存、CPU张量与部署静态检查。CPU模型测试需要Torch2.10、torchvision0.25、Transformers5.2；缺少时明确跳过。CUDA Graph回放测试需NVIDIA GPU。各层验证应独立报告，跳过不计通过。

本次交付不包含Docker构建、真实checkpoint、GPU或硬件验收。原模型以30动作步/秒编码视频时间，而当前ACOne模板频率是100Hz；实际发送还会受RC限速与逐步回调开销影响，消费步数不等于控制周期或墙钟时间。部署时须单独核对RC频率、复位夹爪值和安全设置；策略不会自动修改这些RC参数。动作发送、限速、复位、控制权和清理仍由RC负责。
