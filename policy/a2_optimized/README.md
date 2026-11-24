# a2_optimized

ACOne 视觉历史策略。每次重规划都用本轮三路相机、头相机历史和关节状态生成 50 步动作。默认每生成 4 个新动作块，使用第 4 块的观测在后台刷新一次完整的 Memory、Scene、Subtask 语言段。

| 动作块 | 动作使用的语言条件 | 动作生成后的工作 |
| --- | --- | --- |
| 第 1～3 块 | 无语言段 | 无 |
| 第 4 块 | 无语言段 | 提交第一次语言刷新 |
| 第 5～7 块 | 最近完成的语言段，首次刷新尚未完成时仍为空 | 无 |
| 第 8、12、16…块 | 最近完成的语言段 | 提交本轮刷新 |

仅新预测计数。原动作队列余量充足时直接返回。每次刷新复用本轮三路图像及可选历史视频的视觉编码结果。后续动作只复用语言 token，重新编码自身观测。

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

Monitor 的 `subtask` 显示最近完成的 Subtask；`language_refresh_pending` 包括正在运行与排队的刷新，`language_refresh_completed` 和 `language_refresh_error` 分别记录成功次数与最近的刷新错误。动作延迟沿用 `predict_ms`、`prefix_ms` 和 `denoise_ms`。
