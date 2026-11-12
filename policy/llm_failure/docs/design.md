# llm_failure 设计

新增原生链成员 `llm_failure`，原样返回前序 action_chunk，用 task、当前及历史三路相机图像判断任务是否进入失败状态。不得生成、变换或补齐动作。检测到失败时调用 SDK 0.10 的 `runtime.finish(message, outcome="terminated")`；遵循既有整链提交和 MCP 等待资格语义。

## 已确定行为

- 用户要求每 60 个实际动作 step 检查一次。读取 `runtime.consumed_actions`，以本次上下文首次回调值为基线，累计达到 `check_every_steps=60` 才请求供应商；不是调用次数或时间。
- 首次回调仅保存初始图像并透传。之后在链回调观察到阈值时后台异步检查，长跨度只做一次检查，不补发过期请求；从实际提交步数重新累计 60 步。
- 最多一个在途请求，忙时动作继续透传。上一条完成且已经达到下一阈值时，在当前回调用最新图像补做一次，不排队旧快照。
- 每次 policy 回调先收集后台结果；后台绝不持有 runtime 或调用 finish。true 在后续有效回调请求 finish，错误在该回调抛出。请求绑定本地上下文代次和 control_epoch。
- 默认 `history_size=5`，表示含当前在内最多 5 组图像；每组 head/left_wrist/right_wrist，按执行步数排序。存储独立 JPEG data URL，不保存任何共享内存视图。reset_context、代次变化或计数回退清空历史和计数。
- 非检查轮不编码图像、不访问网络，重复链成员看到相同步数也不重复请求。
- default qpos，可冷配置为 epos/mixed，必须与链首模式一致；非空且合法前序动作由 SDK 校验后原对象返回。独立空输入不构造 hold。
- DeepSeek 返回仅允许 `{"failed":false}` 或 `{"failed":true}`，布尔类型严格检查，拒绝重复键、额外字段、空内容、解释文字、代码围栏和不完整响应。
- true 时调用 finish，message 为固定说明；false 仅透传。API/图像/协议异常走 policy 故障，不伪造失败判断，不重试或改用其他模型。
- 有 MCP return_and_wait 时失败报告会交还 MCP；没有等待任务时只暂停。实现不修改这个既有控制权规则。

## 接口与部署

- 独立包 `rc-llm-failure`，模块 `rc_llm_failure`；主要接口 `FailurePolicy(config, provider=None)`、`policy(action_chunk, images, runtime)`、`reset_context()`、`close()`。
- 独立策略目录 policy/llm_failure，schema3、独立 Python3.11 镜像/容器/host。依赖为 rc-policy-sdk>=0.10、NumPy、Pillow、OpenAI SDK；不引入 SciPy/模型框架到新策略或 SDK。
- 全部参数冷改：task、prompt、check_every_steps(60)、history_size(5)、image_max_side(1024)、jpeg_quality(85)、provider{model,base_url,api_key,timeout_s}。task/key 出厂留空，使用前配置；默认 model=deepseek-flash，base_url=https://api.deepseek.com，timeout_s=30。
- 用户可编辑 prompt；程序附加不可省略的 JSON/失败判断契约。发送 task 与按时序标注的图像；不发送动作、不请求工具调用。
- Chat Completions JSON mode，thinking disabled、temperature=0、max_tokens=128、max_retries=0。一次请求内容上限32MiB，不上传到图床或 Files API。
- `history_size` 范围2–16；图像最大边1–4096、JPEG质量1–95；步数阈值正整数。供应商 URL 禁止内嵌凭证、查询参数、fragment。错误日志不包含 key/原始模型响应/图像数据。

## 选择

用户确认后台异步、最近5次检查、错误停止、忙时空闲后查最新图像、首次执行满60步再查。单执行器网络任务只接收不可变JPEG快照。reset/release 先失效结果再等待旧请求收尾，复用SDK已有异步策略的生命周期屏障；外层host统一绝对期限兜底卡死请求。控制代次变化或计数回退也重新建立基线，旧结果不得成为新代次的判断。

## 验证

验证 action_chunk 对象与内容完全透传，0/59/60/119/120步触发、计数跳跃和重复成员、reset/代次隔离、有界历史与原图修改/释放后的历史独立性。真实SDK+本地HTTP检查DeepSeek请求参数、时序图像和严格JSON/异常处理。真实host/worker/链/MCP验证false继续、true交还、无wait只暂停、图像释放证明。构建独立镜像并核对已安装包；完整主机/Linux套件覆盖现有功能。假供应商测试不声称真实DeepSeek判断准确率或真机验收。
