# π0.5 / LeRobot 策略接入设计

日期：2026-10-28。状态：已完成独立 builder check；用户确认按 Qwen35 的 chunk 裁剪融合实现，进入实施。

## 目标与已知条件

用户希望把 π0.5 作为独立 policy 接入本项目，并已确认持有 **LeRobot 格式的 ACOne 微调权重**。新策略身份为 `pi05`，通过 `switch pi05` 加载，初始化完成后暂停，`resume` 后由 RC 控制线程发送动作。

沿用 schema 3、每策略独立容器/host/执行进程、生命周期退出证明、只读共享图像和代次复核。RC、SDK 与 Qwen 环境不安装 LeRobot/π0.5 依赖。既有未提交的 example 策略及文档/测试不属于本任务。

用户明确表示不清楚训练时的 LeRobot 版本/commit；checkpoint 配置与处理器文件也尚未提供。因此本次以官方稳定版 **LeRobot v0.6.1 / commit `7e241bd630a3719a56157a497ce5d08f244784f1`、Python 3.12** 为初始兼容目标。实现可完成原生接口和可配置映射，不能宣称该实机 checkpoint 已验证可用；不兼容配置明确报错，不默默忽略未知字段。若实际 checkpoint 需要其他版本，依据具体文件另行调整依赖锁和兼容测试。

## 接入路径比较

1. **LeRobot 原生本地推理（选定方向）**：读取用户已有模型配置、权重及保存的前后处理器，保持训练时的归一化流程；无需转换 checkpoint。此方向根据用户明确提供的权重格式选择。
2. OpenPI 原生推理：官方实现完整，但其 checkpoint/config/normalization 体系与用户现有格式不同，需要转换和再次验证，不作为本次实现。
3. 外部推理服务：需要额外服务部署、超时及外部 reader 生命周期协议；当前目标可以用单策略进程完成，不增加此边界。

下面的具体实现选择由本次集成提出，尚不视为用户已确认的硬件/训练语义。

## 文件与部署边界

- `policy/pi05/`：`main.py`、schema 3 `policy.json.example`、`model.json.example`、`compose.yaml`、`deployment.env.example`、Dockerfile 和独立依赖锁。
- `packages/pi05-policy/`：轻量配置预检、LeRobot 后端、单模型异步 chunk 调度。模块顶层不导入 Torch/Transformers。
- `tests/test_pi05_*.py`：配置、输入输出适配、缓存调度、模型生命周期和镜像安装检查。
- `docs/pi05-policy.md`：所需 checkpoint 文件、tokenizer/cache、相机映射、动作语义、构建/切换/诊断和验证边界。

无需给 RC 添加策略名字分支；setup/目录发现自动找到 `pi05`。镜像使用独立 tag，例如 `robot-control-compose-pi05:local`。Python 与 LeRobot/Torch/Transformers 的兼容组合在镜像和锁文件固定；初始目标为上述 v0.6.1 与 Python 3.12。禁止使用随时间漂移的 `main` 作为构建依赖。

模型目录只读挂载 `/models`，tokenizer/HF 缓存单独挂载。维持宿主 UID/GID，并设置 HOME/USER/LOGNAME。初始化只使用预先准备的本地权重和 tokenizer；不在 `switch` 时自动下载模型或安装包。

## 模型加载与配置

`params.model_config_path` 指向策略本地 `model.json`。所有业务字段冷改；`switch-model <path>` 先验证新配置，返回该路径的冷配置补丁，由现有生命周期执行完整重载。

配置至少包含：本地 checkpoint 路径、本地 tokenizer 路径/缓存配置、任务指令、三路相机字段映射、动作表示、状态/动作坐标映射、推理超时和补充动作阈值。禁止借用 Qwen 的 `stats_key`、统计文件和夹爪修正。

加载时读取 checkpoint 的 `config.json`，要求策略类型为 `pi05`，读取保存的 `chunk_size`、`n_action_steps`、feature 定义、归一化方式及模型结构；使用原生 PI05 模型类和配套 `make_pre_post_processors`。明确指定本地文件加载、CUDA 设备和严格权重检查。前后处理器缺失、统计状态缺失或特征不匹配时列出具体文件/字段，不回退到随机权重或无统计归一化。

已核对 v0.6.1 的 `PI05Policy.from_pretrained`：该覆盖方法会捕获部分加载异常后返回模型。因此本项目不能直接依赖其返回值表示加载成功；针对完整 LeRobot checkpoint 使用会传播失败的严格本地 safetensors 加载路径，验证 missing/unexpected keys，保留权重共享语义。此为接入层的明确实现要求，不改写 checkpoint，也不修改上游源码。

本次目标为完整 LeRobot 微调 checkpoint；仅有 PEFT/LoRA adapter 的目录需要先导出完整可加载 checkpoint，或另行明确其加载需求。不自动跨格式转换。

预热在模型线程内执行，结果不进入 RC 动作缓存。编译为可选显式配置，初始默认关闭；不能把预热通过视为真机运动验收。

## 输入、输出与训练语义

输入使用 RC 提供的原始数据：`qpos[14]` 顺序为左臂 6 关节、左夹爪、右臂 6 关节、右夹爪；三路 RGB uint8 图像为 `head/left_wrist/right_wrist`。相机目标 key 由配置明确指定并与 checkpoint 的视觉 feature 精确核对，不按字典顺序猜摄像头身份。

观测在提交后台推理时复制；不得把共享内存视图保留到回调结束之后。把图像转为 LeRobot 约定的张量布局后交给 checkpoint 的预处理器。状态归一化、状态 token 化、tokenizer 和输出反归一化由保存的处理器负责，不能重复执行。

模型原始 action 最大维度可能为 32，最终执行 feature 必须是 14 维双臂关节指令。使用原生接口依据 checkpoint 的 action feature 去掉 padding，不任意截取一个其他机器人策略的前 14 维。任何输出必须是有限的 `[H,14]`。

动作表示必须明确为 RC 坐标系的绝对关节目标，或相对本次观测的关节增量。若保存的处理器已经完成 relative→absolute，适配层不得再次叠加。自定义训练但未保存相对动作转换时，使用显式的关节 delta 模式，夹爪保持绝对。坐标/单位转换为明确的配置；默认 identity 仅代表训练数据与 RC 使用同一 ACOne 定义，不推断 ALOHA 的翻转与夹爪公式。文档要求用户核对 checkpoint 的实际训练约定。

不实现 LeRobot 机器人驱动，不让模型直接发送 ROS 指令。暂不启用 LeRobot RTC 或历史视觉/状态记忆；遇到要求这些模式的配置须明确拒绝，避免与本项目动作调度重复补偿或错误维护历史。

## action chunk 调度

用户明确要求 chunk 裁剪融合套用 Qwen35。把 Qwen35 现有通用调度实现抽到 SDK 的轻量 `AsyncChunkPolicy`，两边复用同一份代码；SDK 仅增加标准库/NumPy 逻辑，不引入模型依赖。Qwen35 的固定 50/30 和专属夹爪后处理保持原行为，π0.5 调用同一裁剪/融合实现而不应用该偏移。

使用独立的单线程模型执行器，串行完成 load/warmup/predict/reset/release。策略声明 sync 回调、每完整动作回调一次；昂贵的推理在策略内部异步进行。

每次调用原生 `predict_action_chunk`，按 checkpoint 的 `n_action_steps` 选择可执行前缀，并验证 `0 < n_action_steps <= chunk_size`。补充阈值可配置，默认按可执行长度的 60% 取整并限制在有效范围内；50 步配置对应少于 30 步触发补充。首次空缓存等待第一批；旧缓存耗尽时等待现有任务，不重复发起第二个模型任务。

收集结果时以 `consumed_actions` 对齐，丢弃已过时前缀，在完整目标边界安装结果。使用 SDK 的 NumPy `TemporalTransition` 做重叠区间指数融合并保留旧夹爪计划。π0.5 不执行 Qwen 专属的 `> -2.0` 后 `+0.15` 修正。

暂停/归还/重载沿用 `control_epoch`；旧代次结果不进入新缓存。错误或推理截止期限超时正常上报 policy 故障。release 挂起由既有进程树/容器清理期限兜底，不通过后台线程失联宣称已释放图像或 GPU。

## 验证与完成标准

1. 轻量预检覆盖缺文件、错误策略类型、非 14 维 feature、相机映射、不可用模式、无效阈值和相对动作转换冲突。
2. 后端测试验证真实 LeRobot 配置/处理器序列化与重载、输入布局、归一化/反归一化和动作形状；模型推理可用受控替身测试接口边界，但须明确标注。
3. 调度测试覆盖 50/30 默认、其他有效长度、推理期间消费裁剪、全重叠融合、夹爪不加 Qwen 偏移、快照复制、过期代次、超时、reset/release。
4. 实际策略 worker/容器 smoke 验证目录发现、initialize/step/reset/release、共享图像释放和安装后的模块路径。阶段边界运行完整项目测试。
5. 构建验证检查已安装 LeRobot 版本、设备/依赖版本和原生模型导入，不依赖宿主源码路径。
6. 实机验收单独要求用户提供 checkpoint 与 NVIDIA GPU：加载、真实三相机输入、输出单位和范围检查、暂停状态下推理检查，最后才进入现场控制验证。当前本地 CPU/mock/Docker 测试不能替代这些证据。

## 官方依据

- [OpenPI 官方仓库与权重格式](https://github.com/Physical-Intelligence/openpi/tree/215abfb217dbac7d5f1273282331b9b1866c0479)
- [LeRobot π0.5 官方文档](https://huggingface.co/docs/lerobot/pi05)
- [LeRobot 官方发布版本](https://github.com/huggingface/lerobot/releases)
- [PI05 原生配置](https://github.com/huggingface/lerobot/blob/7e241bd630a3719a56157a497ce5d08f244784f1/src/lerobot/policies/pi05/configuration_pi05.py)
- [原生前后处理器加载与相对动作状态重连](https://github.com/huggingface/lerobot/blob/7e241bd630a3719a56157a497ce5d08f244784f1/src/lerobot/policies/factory.py)

研究时最新稳定版为 v0.6.1，main 已标为 0.6.2；本方案锁定稳定版，不把研究 main 当作用户 checkpoint 的版本。
