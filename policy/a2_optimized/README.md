# a2_optimized

ACOne 视觉历史策略。每次重规划都用本轮三路相机、头相机历史和关节状态生成 50 步动作。默认每生成 4 个新动作块，使用第 4 块的观测在后台刷新一次完整的 Memory、Scene、Subtask 语言段。

| 动作块 | 动作使用的语言条件 | 动作生成后的工作 |
| --- | --- | --- |
| 第 1～3 块 | 无语言段 | 无 |
| 第 4 块 | 无语言段 | 提交第一次语言刷新 |
| 第 5～7 块 | 最近完成的语言段，首次刷新尚未完成时仍为空 | 无 |
| 第 8、12、16…块 | 最近完成的语言段 | 提交本轮刷新 |

仅新预测计数。原动作队列余量充足时直接返回。每次刷新复用本轮三路图像及可选历史视频的视觉编码结果。后续动作只复用语言 token，重新编码自身观测。

A2 将选中的头相机历史帧按原顺序组成一个 uint8 视频，再整批转换为 float32 并除以 255；时间戳仍取各帧的原始索引。视觉编码器把 token 数相同的切片放入同一次 SDPA 调用，每个切片独立计算注意力，并按输入顺序拼回输出。该路径用于动作、后台语言刷新及启用的前缀 CUDA Graph。

语言预填充及后续逐 token 解码与动作共用一个模型执行线程。动作回包独立于后台刷新，后续动作请求可以在预填充或单个 token 计算结束后插入。刷新按触发顺序执行，整段完成后才替换缓存。生成失败会保留上次完整结果，并记录错误。切任务或 reset 清空缓存、待处理刷新和计数；普通 resume 保留已完成语言与计数，并丢弃未完成刷新。每个策略实例持有自己的缓存。

## 使用

```bash
./setup.sh --config-only
# 在 policy/a2_optimized/ 下配置 deployment.env 和 model.json
./setup.sh --policies a2_optimized
```

在 TUI 切换并启用异步调度：

```text
policy switch a2_optimized
policy a2_optimized config set execution.mode async
resume
```

模板继承 history 的 sync 调度默认值。模型权重、图像处理、去噪 CUDA Graph、动作融合和夹爪规则沿用 [qwen35_history](../qwen35_history/README.md)。本策略与 history 共用依赖镜像和只读模型源码，配置和执行容器各自独立。修改 `model_config.inference.generate_language` 可以选择语言字段，空列表会关闭刷新，并允许使用原有前缀 CUDA Graph。

A2 从基础 Qwen 目录读取配置和 tokenizer，并直接从 A2 checkpoint 将权重装入目标设备。Monitor 的 `checkpoint_load_path=single_checkpoint` 表示启用了这条路径。`legacy` 表示使用兼容加载，例如旧 checkpoint 同时保存了输入和输出词表的独立权重。

## 动作块融合

`model.json` 的 `execution.pipelined` 控制是否融合，`transition.trunk_mode` 选择接点，模板沿用 `tail`。

- `close` 在旧剩余块与新块之间找全局最近点。
- `tail` 固定旧块最后一点，在新块里找最近点。按默认的 `trunk_smoothing=true`，从接点向前对齐，用指数权重融合重叠段，然后直接接上新块后续动作。

在异步调度下，独立运行的 A2 或原生链末环的 A2 会把新预测交给 RC。RC 收到回包时，用当时仍可替换的旧动作重新找接点并融合。已提交或已开始限速执行的目标保留，启用平滑时第一条可替换目标沿用旧轨迹。若旧段已无可替换动作，RC 按请求期间的执行进度裁剪新块。A2 在原生链的中间环节时，会先生成融合后的动作供后续环节处理。

这两个配置项可在运行中提交，从后续策略请求生效，保留模型、视觉历史和语言缓存。正常的 RC 配置版本和代次校验仍适用。

```text
policy a2_optimized config set model_config.execution.pipelined true
policy a2_optimized config set model_config.transition.trunk_mode close
policy a2_optimized config set model_config.transition.trunk_mode tail
policy a2_optimized config save
```

`set` 更新当前实例，`save` 写回当前模型 JSON。直接编辑模型 JSON 后，在下次加载时读取。已有部署可用 `./setup.sh --config-only` 刷新命令补全提示。旧 A2 模型文件若使用 `now`，加载前请改成 `close` 或 `tail`。重复实例使用确切实例名。

## Monitor

`subtask` 显示最近完成的 Subtask。`language_refresh_pending` 含运行中和排队的刷新，`language_refresh_completed` 与 `language_refresh_error` 分别记录成功次数和最近错误。

| 字段 | 最近一次成功记录，单位 ms |
| --- | --- |
| `action_ms` | 动作推理总耗时 |
| `action_processor_ms`、`action_prefix_ms`、`action_denoise_ms`、`action_post_ms` | 动作的输入准备、前缀、去噪和模型输出转换 |
| `subtask_ms`、`subtask_queue_ms` | 整轮语言刷新墙钟耗时、提交至首个切片开始 |
| `subtask_prepare_ms`、`subtask_prefill_ms`、`subtask_decode_ms`、`subtask_post_ms` | 冷启动模板准备、预填充、逐 token 解码和文本解析 |

动作耗时继续提供原字段。语言指标覆盖配置启用的整段 Memory、Scene、Subtask。CUDA 上的预填充和解码按各执行切片计时，`subtask_ms` 还包含切片之间让给动作推理的等待。
