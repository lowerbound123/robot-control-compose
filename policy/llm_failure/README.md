# 异步视觉失败检测策略

`llm_failure` 放在生成动作的 policy 后面，原样返回前序 action chunk，不生成动作、不修改数值或控制模式。它在独立 Python 3.11 容器中使用 DeepSeek 或 GPT Decisions 检查 task 是否已进入失败状态。

## 60 步与图像历史

步数来自 RC 的 `runtime.consumed_actions`：只有一个完整动作目标成功发送并消费，才增加一步。它不是控制循环次数、policy 回调次数或经过的秒数。首次回调记录基线和初始三路图像，不调用 API；累计执行 60 步后，在下一次链回调中发起首个检查。

最多一个后台请求。等待供应商时仍立即返回前序动作。若上一请求结束时又执行了至少 60 步，下一次回调用当时最新图像补做一次；不排队旧快照，也不为跨过的多个阈值补发请求。下一阈值从实际提交时的步数重新累计。链调用本身较慢时，检查步数可能超过 60 的精确倍数。

每次检查取最近最多 5 组图像（含当前），每组有 `head`、`left_wrist`、`right_wrist`，按相对执行步数排列。初始图像参与前几次判断，超过容量后丢弃最早组。只有初始与实际提交检查时编码图像，普通透传回调不编码。

每组历史保存自有 JPEG 和采样时展开的图像标注。请求提交后，后台线程使用冻结的输入完成 HTTP 调用；有效结论由后续策略回调提交。

## 判断结果与控制权

DeepSeek 请求 JSON mode 并关闭 thinking，响应须为仅含一个布尔字段 `failed` 的 JSON 对象。GPT Decisions 每次向 `/v1/decisions` 发送一道题，`type` 决定题型。三种配置均填写 `question_name`，回包必须恰好有一个题名和类型均匹配的答案。

| provider 的 `type` | 题型配置 | 终止规则 |
|---|---|---|
| `gpt-decision-predicate` | 用 `prompt` 和 `response_instructions` 描述待判断的条件 | 用返回的 `probability` 和 `fail_if` 比较，范围为 0 到 1。出厂配置在失败概率 ≥ 0.8 时终止。 |
| `gpt-decision-choice` | `choices` 按顺序列出 2–255 个不同的选项。每项填写 `value`、布尔值 `failed` 和可选的 `description` | 返回值按类型和值匹配，选中 `failed: true` 的任意一项即终止。YAML 的布尔值 `true` 与字符串 `"true"` 是两项不同的选择。 |
| `gpt-decision-score` | `levels` 按顺序列出 2–10 个等级，每级填写 `label` 和可选的 `description` | `score` 是从 0 开始的等级序号的概率加权平均。三级量表的范围为 0 到 2，出厂配置在分数 ≥ 1.5 时终止。 |

数值规则写作 `fail_if: {op: '>=', threshold: 0.8}`，也支持 `'<='`。等于阈值时命中，小数直接参与比较。`fail_if` 和选项的 `failed` 由策略在本地使用。拒答、错题名、未配置的选项、非有限数和越界数值均按供应商请求失败处理。[Decisions 题型和评分说明](https://developers.openai.com/api/docs/guides/decisions)

`failed=true` 在下一次有效 `policy()` 回调调用：

```python
runtime.finish("llm_failure：<当前供应商> 判断任务已进入失败状态", outcome="terminated")
```

之后仍返回合法的前序动作；RC 在整链成功和图像释放确认后丢弃该轮动作并暂停。建议把检查器放在链尾，缩短结果提交前的等待。后台检查期间动作继续，检查器不是独立的即时急停通道。

- 有 `return_and_wait` 任务时：交还 MCP，回执 `status=succeeded`、`result.outcome=terminated`，附带固定 message 和来源位置。
- 没有等待任务时：暂停并保留 policy 所有权。
- API 超时、断连、拒答、HTTP 错误或违反响应契约：本次检查失败，继续原样透传动作；不会卸载、锁死、暂停或结束任务，MCP 等待任务继续运行。累计供应商失败次数会显示在实例监控中。
- 本地配置、动作/图像校验、请求超过 32 MiB 或程序异常仍为策略故障。

SDK 对同一查询不自动重试，不切换模型或接口。供应商失败后，仍按 `check_every_steps` 从上次实际提交步数累计；满足间隔且旧查询真正结束时，下一次回调使用最新图像发起一次新检查。每个请求有从提交时开始计算的所选后端 `timeout_s` 预算，后续回调检查超时；迟到响应不能成为有效判断。超时但尚未结束的请求仍占用唯一在途位置，不会并发补发。暂停/恢复、MCP 归还及 reset 会清空历史和计数并丢弃旧结果。reset/release 先使结果失效，再等待旧请求收尾，卡死请求由现有 host 的统一生命周期期限和进程树清理兜底。

## 配置

```bash
./setup.sh --policies llm_failure
```

在 `params.provider_config_path` 指向的供应商文件中填写 `task` 和 `api_key`。每个供应商有自己的任务、判断提示和图像说明，每个实例只读取所选文件。初始化和切换预检只做本地校验。

| policy.yaml 中的参数 | 默认值 / 要求 |
|---|---|
| `check_every_steps` | 60，正整数 |
| `history_size` | 5，范围 2–16，包含当前组 |
| `image_max_side` | 1024，范围 1–4096，保持比例，不放大小图 |
| `jpeg_quality` | 85，范围 1–95 |
| `provider_config_path` | 默认 `provider.yaml`，相对本策略来源目录，也可用容器内可见的绝对路径 |

DeepSeek 默认使用 `provider.yaml`。三个 Decisions 模板各自对应一个 `type`，可分别填写凭证、模型、任务和提示。四份配置都使用 `model`、`base_url`、`api_key`、`timeout_s` 以及下表中的文字字段。[DeepSeek 模板](provider.yaml.example) · [predicate 模板](providers/gpt-decision-predicate.yaml.example) · [choice 模板](providers/gpt-decision-choice.yaml.example) · [score 模板](providers/gpt-decision-score.yaml.example)

| 供应商字段 | 如何使用 |
|---|---|
| `task` | 必填，填写这个供应商要检查的机器人任务。 |
| `prompt`、`response_instructions` | 判断规则与输出说明。两段原样以一个空行连接，送入 Chat 的 system 消息或 Decisions 的问题指令。可以把全部指令写进 `prompt`，此时将 `response_instructions` 设为 `''`。文字中的 JSON 花括号按原文发送。 |
| `input_templates.task` | 第一段输入文本，必须包含 `{task}`。 |
| `input_templates.history_snapshot`、`input_templates.current_snapshot` | 历史帧组与当前帧组的标注，可引用从 1 开始的 `{index}` 和实际执行步数 `{step}`。 |
| `input_templates.camera` | 每张图像前的标注，可引用相机名 `{camera}`。输入模板如需显示字面花括号，写成 `{{` 和 `}}`。 |
| `choices[].description`、`levels[].description` | 各选项或等级的可选说明。题名、`value`、`label` 与本地终止规则在加载 provider 时确定。 |

### 提示词变量

`{$变量名}` 可用于任务、判断指令、输出说明、所有图像输入模板和选项／等级的说明。例如，共享字典中 `time=20`、`phase="抓取"` 时，`time={$time}，阶段={$phase}` 会发送为 `time=20，阶段=抓取`。

- `{$qpos}`、`{$qvel}`、`{$effort}`、`{$timestamp}` 取本次采样的同名机器人反馈。`{$epos}` 和 `{$eef}` 均取实测的 `state["eef"]`。这些名字优先，即使反馈缺失也不会改用同名共享值。
- 其他变量按顶层键从同一份共享字典快照读取，命名规则是 `[A-Za-z_][A-Za-z0-9_]*`。字符串原样发送，其余值使用紧凑 JSON 并保留中文。缺键或无法序列化时填空串。`false`、`0` 和 `null` 各自保留。共享快照读取失败时，仅本次采样的共享变量为空。
- 在采样回调内每帧组最多读一次共享字典。历史图像及其相机标注保留当时的值，当前任务和问题描述取提交时的值。即使共享字典随后改变，已采样的变量也不会重新展开。插入值里的 `{$other}`、`{task}` 或 JSON 花括号均是普通文字。`{index}` 仍是该帧组在本次请求中的位置。
- 例如把 `history_snapshot` 写成 `'历史 {index}，步数 {step}，姿态 {$qpos}，阶段 {$phase}'`。阶段后来变为“放置”时，原图旁仍是“抓取”。窗口淘汰、reset 或卸载会移除本轮历史引用，已在途的请求可使用自己的快照直到完成。

### 升级与切换

`llm_failure` 的旧 `type: openai_decisions` 和 `choice_descriptions` 需要手工修改。沿用旧二选一行为时，将类型设为 `gpt-decision-choice`、题名设为 `failed`，把两个旧说明按顺序写入 `choices`，分别填写 `value: not_failed, failed: false` 和 `value: failed, failed: true`。可保留原文件路径。

使用当前源码挂载部署和已锁定依赖时，这次更新可沿用现有镜像。如果策略已在运行，更新源码和选定的配置后执行一次 `policy switch <当前链名>`，让策略进程加载新实现。此后通过 `switch-provider` 切换题型或重读同一配置文件。

如果旧部署仍将 `params.task` 和 `params.prompt` 保存在策略清单中，先手工更新旧 Decisions 题型，再用 setup 所用的 Python 运行已有的提示词迁移命令。迁移与 setup、手动编辑分开执行。

```bash
python3 policy/llm_failure/scripts/migrate_prompts.py
```

工具把旧 `params.task` 和 `params.prompt` 迁入各供应商文件，按后端补齐模板，将已有文件的变更前副本放入 `.setup/backups/`。它会识别策略目录和子目录中的备用配置，用旧任务和提示填补缺失或仍为出厂默认值的字段，保留各供应商已经定制的值。跳过目录中的本地配置可用 `--profile` 指定。标准容器路径 `/workspace/policy/llm_failure/…` 会映回本地策略目录，其他外部挂载文件按相应的 `.example` 手动迁移。

填写所选文件的密钥后，可在 TUI 中切换。如果链中有两个失败检测实例，以下命令对第二个生效：

```text
policy llm_failure-1 switch-provider providers/gpt-decision-choice.yaml
policy llm_failure-1 config save
resume
```

只有一个实例时使用 `llm_failure`。候选文件校验失败时，原实例继续使用已加载的设置。校验成功后 RC 只冷重载目标实例并暂停整链，其他成员保留现有模型和路径。若新实例初始化失败，修正文件后可执行 `policy llm_failure-1 retry`。再次指定同一路径会重读文件。`policy <实例> config save` 是可选步骤，只将选定路径写回来源策略的 `policy.yaml`。该文件是以后创建实例时的默认配置。

默认 `control_mode=qpos`，可改为 `epos` 或 `mixed`，但必须与链首策略一致。检查器要求非空前序动作，因此应作为链成员使用，不能单独启动后依靠它生成保持动作。

供应商接收展开后的指令与带标注的图像。只有模板明确引用的反馈量和共享变量才会进入请求。请求内容上限 32 MiB，超限明确失败，请减小历史数量、图像尺寸或质量；不上传到图床或 Files API。诊断日志保存有界的供应商原始响应，输出前会脱敏配置密钥及 Authorization 凭证；日志不包含请求图片、task、prompt 或配置凭证。

## 实例监控

通过 `monitor llm_failure` 打开单实例监控；重复实例使用明确名称，例如 `monitor llm_failure-1`。`monitor(context)` 只读取内存中的六个标量，不执行 HTTP、文件或控制操作，也不等待供应商请求、诊断输出或 reset 收尾。

| 字段 | 当前 episode 内的含义 |
|---|---|
| `in_flight` | 已提交且后台尚未真正结束的请求数，包含超时仍在运行的请求；完成但尚未采集的结果为 0 |
| `avg_http_s` / `max_http_s` | 已结束请求真实 `http_duration_s` 的平均值 / 最大值，仅统计 HTTP `.create()` 耗时，不包含图像编码、解析和等待采集 |
| `avg_total_tokens` / `max_total_tokens` | 供应商实际 `usage.total_tokens` 样本的平均值 / 最大值；不使用输入/输出 token 求和或估算 |
| `failed_requests` | 超时、网络、HTTP 状态、拒答或无效响应造成的检查失败次数，每个查询最多计一次；合法 `failed=true` 不计作请求失败 |

HTTP 耗时或 total token 没有样本时显示 `—`；缺失样本不按 0 纳入平均值。失败响应若带有有效 HTTP 耗时或 total token，仍纳入对应样本。每次 `reset_context()` 切换代次时立即清空累计值；旧请求随后完成也不会污染新 episode，`in_flight` 则始终反映后台实际收尾状态。每个运行实例独立累计，以常数空间、常数时间维护。

## 请求与采集诊断

策略独立输出单行 JSON 事件，`event` 始终以 `llm_failure.` 开头。启用 RC 录制时，这些策略日志会另存到该次录制的 `llm_failure.jsonl`。直接使用 `ProviderClient(load_provider(...))` 时，调用 `decide(snapshots)` 即可得到布尔判断。构造时可传入 `diagnostics` 回调收集脱敏元信息。

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
| `provider`、`model`、`response_model`、`provider_request_id`、`http_status` | 所选 provider 的完整 `type`、请求模型、响应模型、供应商请求 ID 与 HTTP 状态；不可取得时为 `null` |
| `usage`、`finish_reason` | 响应 token 统计与终结原因；DeepSeek 保留 prompt/completion 及缓存计数，Decisions 保留 input/output/total 及缓存、reasoning 明细（供应商有返回时）；Decisions 的 finish_reason 为 predicate、choice、score 或 refusal 等答案类型，原始响应保留其他附加字段 |
| `raw_response`、`parsed_failed`、`error` | 脱敏响应正文、解析布尔值、脱敏错误；JSON 响应正文规范化为 JSON 字符串，非 JSON 正文保留文本 |
| `late`、`invalidated` | 是否超出提交期限、是否已因上下文变化或已报告的超时而失效 |
| `truncated`、`truncated_fields` | 是否因长度上限截断及对应字段路径 |
| `original_size_bytes`、`raw_response_original_size_bytes` | 截断前脱敏事件载荷的 UTF-8 字节数（不含截断标记）及脱敏响应正文的字节数 |

每条完整 JSON 行（含换行）最多 8000 个 UTF-8 字节，长度计算包含 JSON 转义开销。普通 128 token 响应完整保留；异常长响应按整行预算保留前缀并标记截断，截断后的 `raw_response` 不保证仍能作为独立 JSON 解析。两个策略线程共用输出锁，避免它们各自的 JSON 行交错；诊断输出或可选回调失败不改变布尔判断、策略故障和清理结果。

## 链与 MCP 用法

先准备动作策略，将其与 `llm_failure` 通过 `policy manage` 编排为同模式的链。例如保存的链定义可以是：

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

不指定 `timeout_s` 时会一直等待。MCP 调用超时或断连后，原任务继续执行，随后可以查询结果。

## 验证与边界

单元测试覆盖动作原对象透传、实际步数与忙碌合并、历史文字冻结、代次隔离、严格 JSON 以及三种 Decisions 回包。本地 HTTP 服务测试使用真实 OpenAI 兼容客户端；真实 SDK host/worker/链测试覆盖异步请求期间图像释放、完成回调和 MCP 所有权。Docker 验收使用隔离的 mock 机器人与本地模型替身。

这些测试证明调用与控制协议，不证明真实供应商的失败判断准确率或真机表现。部署后需以实际 task 和相机画面评估误判与漏判。

接口参考：[OpenAI Decisions](https://developers.openai.com/api/reference/resources/decisions/methods/create)、[OpenAI Python Decisions 类型](https://github.com/openai/openai-python/blob/main/src/openai/types/decision_create_params.py)、[DeepSeek Vision](https://api-docs.deepseek.com/guides/vision/)、[JSON Output](https://api-docs.deepseek.com/guides/json_mode/)、[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。
