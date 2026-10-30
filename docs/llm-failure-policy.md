# 异步视觉失败检测策略

`llm_failure` 放在生成动作的 policy 后面，原样返回前序 action chunk，不生成动作、不修改数值或控制模式。它在独立 Python 3.11 容器中使用 DeepSeek 检查 task 是否已进入失败状态。

## 60 步与图像历史

步数来自 RC 的 `runtime.consumed_actions`：只有一个完整动作目标成功发送并消费，才增加一步。它不是控制循环次数、policy 回调次数或经过的秒数。首次回调记录基线和初始三路图像，不调用 API；累计执行 60 步后，在下一次链回调中发起首个检查。

最多一个后台请求。等待 DeepSeek 时仍立即返回前序动作。若上一请求结束时又执行了至少 60 步，下一次回调用当时最新图像补做一次；不排队旧快照，也不为跨过的多个阈值补发请求。下一阈值从实际提交时的步数重新累计。链调用本身较慢时，检查步数可能超过 60 的精确倍数。

每次检查取最近最多 5 组图像（含当前），每组有 `head`、`left_wrist`、`right_wrist`，按相对执行步数排列。初始图像参与前几次判断，超过容量后丢弃最早组。只有初始与实际提交检查时编码图像，普通透传回调不编码。

历史只保存独立 JPEG data URL，不保存共享内存视图。后台线程只做 HTTP 请求并存储结果；它不会调用 `runtime.finish()`、发布动作或操作 Docker。

## 判断结果与控制权

模型只能返回 `{"failed":false}` 或 `{"failed":true}`，字段必须是布尔值，不允许说明文字、代码围栏、重复键或额外字段。请求启用 JSON mode、关闭 thinking，不要求推理过程。系统提示词明确说明：正常过渡、尚未完成、暂时遮挡或证据不足不能单独判定失败。

`failed=true` 在下一次有效 `policy()` 回调调用：

```python
runtime.finish("llm_failure：DeepSeek 判断任务已进入失败状态", outcome="terminated")
```

之后仍返回合法的前序动作；RC 在整链成功和图像释放确认后丢弃该轮动作并暂停。建议把检查器放在链尾，缩短结果提交前的等待。后台检查期间动作继续，检查器不是独立的即时急停通道。

- 有 `return_and_wait` 任务时：交还 MCP，回执 `status=succeeded`、`result.outcome=terminated`，附带固定 message 和来源位置。
- 没有等待任务时：暂停并保留 policy 所有权。
- API 超时、断连、拒答、HTTP 错误或无效 JSON：抛出策略故障，不伪造 `failed=true`；有等待任务时回执为 `failed` 并交还 MCP。

请求不自动重试，不切换模型或接口。每个请求有提交时开始计算的 `provider.timeout_s` 预算，后续回调检查超时；迟到响应不能成为有效判断。暂停/恢复、MCP 归还及 reset 会清空历史和计数并丢弃旧结果。reset/release 先使结果失效，再等待旧请求收尾，卡死请求由现有 host 的统一生命周期期限和进程树清理兜底。

## 配置

```bash
./setup.sh --policies llm_failure
```

编辑生成的 `policy/llm_failure/policy.yaml`，必须填写 `params.task` 和 `params.provider.api_key`。出厂留空，初始化时明确拒绝；初始化不会联系 DeepSeek。所有参数均为冷配置，修改后重新加载运行组。

| 字段 | 默认值 / 要求 |
|---|---|
| `task` | 必填任务文本 |
| `prompt` | 可编辑的失败判断提示词；程序追加固定 JSON 契约 |
| `check_every_steps` | 60，正整数 |
| `history_size` | 5，范围 2–16，包含当前组 |
| `image_max_side` | 1024，范围 1–4096，保持比例，不放大小图 |
| `jpeg_quality` | 85，范围 1–95 |
| `provider.model` | `deepseek-flash` |
| `provider.base_url` | `https://api.deepseek.com` |
| `provider.api_key` | 必填，不使用全局环境凭证 |
| `provider.timeout_s` | 30，有限正数 |

默认 `control_mode=qpos`，可改为 `epos` 或 `mixed`，但必须与链首策略一致。检查器要求非空前序动作，因此应作为链成员使用，不能单独启动后依靠它生成保持动作。

供应商只接收 task、prompt 和带相机/步数标注的图片，不接收动作块。请求内容上限 32 MiB，超限明确失败，请减小历史数量、图像尺寸或质量；不上传到图床或 Files API。诊断日志保存有界的供应商原始响应，输出前会脱敏配置密钥及 Authorization 凭证；日志不包含请求图片、task、prompt 或配置凭证。

## 请求与采集诊断

策略独立输出单行 JSON 事件，`event` 始终以 `llm_failure.` 开头。启用 RC 录制时，这些策略日志会另存到该次录制的 `llm_failure.jsonl`；诊断本身持续输出，不需要给策略新增启停命令。直接使用 `ProviderClient` 时默认不输出日志，调用者可以传入可选的 `diagnostics` 回调收集脱敏元信息，`decide()` 仍只返回布尔值，HTTP 请求数量与重试设置不变。

每次实际发起检查生成唯一的 `query_id`，由请求、采集和清理事件共同引用。`instance_id` 来自运行配置的 `id`，因此重复出现的策略实例仍能独立关联。供应商返回的请求标识单独存放在 `provider_request_id`，不与本地查询 ID 混用。

| 事件 | 含义 |
|---|---|
| `llm_failure.request.started` | 后台线程开始执行这次查询 |
| `llm_failure.request.completed` | 后台查询成功取得合法布尔判断，尚未表示 RC 已采纳 |
| `llm_failure.request.error` | 后台查询异常、HTTP 错误、拒答或违反响应契约 |
| `llm_failure.collection.accepted` | 有效代次内、期限内完成的布尔结果已在策略回调中采集 |
| `llm_failure.collection.discarded` | 结果因 reset、close、代次变更、超时或供应商错误被丢弃；`reason` 说明原因 |
| `llm_failure.collection.timeout` | 采集时请求仍未完成且期限已到，仅记录一次；`pending=true` |

即使请求在 reset、close 或采集超时后才结束，后台线程仍记录自己的终结事件，收尾时再记录丢弃事件；旧结果不会调用 `runtime.finish()`。进程树被强制终止或输出通道已失效时，进程内诊断无法保证写出终结行。

| 字段 | 含义 |
|---|---|
| `query_id`、`instance_id` | 本地查询及运行实例身份 |
| `step`、`history_size`、`history_steps` | 提交时的相对已执行步数、图像历史组数及每组步数 |
| `epoch`、`generation` | 提交时的控制代次及检查器上下文代次 |
| `submitted_wall_s`、`submitted_mono_s`、`deadline_mono_s` | 提交的 Unix 时间、单调时间及绝对期限 |
| `started_wall_s`、`started_mono_s`、`finished_wall_s`、`finished_mono_s`、`elapsed_s` | 后台查询开始、结束时间及查询总耗时 |
| `http_started_wall_s`、`http_started_mono_s`、`http_finished_wall_s`、`http_finished_mono_s`、`http_duration_s` | 真实 HTTP 调用两端的时间及耗时，在 `.create()` 周围测量，不包括解析响应或等候下次策略回调 |
| `collected_wall_s`、`collected_mono_s`、`collection_wait_s` | 采集时间及后台完成到采集之间的等待；请求尚未完成时等待值为 `null` |
| `provider`、`model`、`response_model`、`provider_request_id`、`http_status` | 供应商、请求模型、响应模型、供应商请求 ID 与 HTTP 状态；不可取得时为 `null` |
| `usage`、`finish_reason` | 响应 token 统计与终结原因；原始响应中也保留供应商附加字段 |
| `raw_response`、`parsed_failed`、`error` | 脱敏响应正文、解析布尔值、脱敏错误；JSON 响应正文规范化为 JSON 字符串，非 JSON 正文保留文本 |
| `late`、`invalidated` | 是否超出提交期限、是否已因上下文变化或已报告的超时而失效 |
| `truncated`、`truncated_fields` | 是否因长度上限截断及对应字段路径 |
| `original_size_bytes`、`raw_response_original_size_bytes` | 截断前脱敏事件载荷的 UTF-8 字节数（不含截断标记）及脱敏响应正文的字节数 |

每条完整 JSON 行（含换行）最多 8000 个 UTF-8 字节，长度计算包含 JSON 转义开销。普通 128 token 响应完整保留；异常长响应按整行预算保留前缀并标记截断，截断后的 `raw_response` 不保证仍能作为独立 JSON 解析。两个策略线程共用输出锁，避免它们各自的 JSON 行交错；诊断输出或可选回调失败不改变布尔判断、策略故障和清理结果。

## 链与 MCP 用法

先准备动作策略，将其与 `llm_failure` 通过 `manage-policy` 编排为同模式的链。例如保存的链定义可以是：

```json
{"schema_version":3,"kind":"chain","policies":["qwen35_single","llm_failure"]}
```

链由首成员决定 RC 的同步/异步调度设置，检查器的 HTTP 请求始终在其自己的后台线程中执行。重复的 `llm_failure` 成员按出现顺序使用 `llm_failure-0/1/2` 等独立实例，各自保持历史、请求与诊断身份。

完成链加载后使用：

```text
mcp take
mcp return_and_wait {"timeout_s":30}
mcp get_policy_result
```

不指定 `timeout_s` 时一直等待；MCP 调用超时或断连不会取消任务，随后可以查询结果。需要 SDK/RC 0.10 或更新版本，更新源代码后重建 RC 和该策略镜像。原 `llm_direction` 不受影响。

## 验证与边界

单元测试覆盖动作原对象透传、实际步数与忙碌合并、不可变历史、代次隔离和严格 JSON。本地 HTTP 服务测试使用真实 OpenAI 兼容客户端；真实 SDK host/worker/链测试覆盖异步请求期间图像释放、完成回调和 MCP 所有权。Docker 验收使用隔离的 mock 机器人与本地模型替身。

这些测试证明调用与控制协议，不证明真实 DeepSeek 的失败判断准确率或真机表现。部署后需以实际 task 和相机画面评估误判与漏判。

接口参考：[DeepSeek Vision](https://api-docs.deepseek.com/guides/vision/)、[JSON Output](https://api-docs.deepseek.com/guides/json_mode/)、[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。
