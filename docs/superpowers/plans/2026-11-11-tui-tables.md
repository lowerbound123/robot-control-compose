# TUI 通用表格显示与 policy 输出接口

本计划由用户批准。复用当前 `codex/tui-statusbar` 分支及工作目录，保留已有未提交状态栏改动和私有配置，不自动提交、推送或操作远端部署。实现及独立审查均使用 gpt-6-astra / max，共享文件按阶段交接。唯一进度账本为忽略目录 `.codex/task-state.md`，运行证据放临时目录，不提交。

## 目标与范围

在现有日志区加入通用表格，首批覆盖 `connector list`、`policy list`、`status`、`connector status`、`record status`；帮助目录和参数复用排版能力。policy 同时支持自定义命令返回表格以及初始化、推理、观测期间主动输出表格日志。普通业务结果保持原有 JSON 结构，整条复制保留原始 JSON；帮助继续复制完整帮助文字。

采用显式结构化数据，不从普通 JSON、Markdown 或 print 猜测表格。不增加独立面板、排序或筛选。上一轮固定状态栏保持不变。

## SDK 契约与传输

新增标准库实现的 `Table(title="", columns=[...], rows=[[...]])` 与 `TableOutput(result=raw_result, tables=[table])`。命令直接返回 TableOutput；主动日志使用 `runtime.log_table(TableOutput(...), level="info")`。

- result 必填，tables 支持多张有序表格；列名和标题是普通文本，行宽与列数一致，单元格接受有限 JSON 值，嵌套值显示完整 JSON 文本。
- 发送前校验并复制数据，不执行回调、Rich 标记或表达式；仅识别显式包装类型，普通同名字段字典保持原样。
- command_result 拆成原 result 和独立 table_document。operation/MCP 的业务结果继续使用原 result；带配置/生命周期 effect 的命令最终成功后才显示表格，失败沿用原语义。
- 日志事件携带可读纯文本 message、可选 table_document 和整条复制用原始 JSON；headless、日志、录制保留完整内容和既有脱敏规则。
- log_table 不等待 socket 或确认，bool 返回值只表示本级入队。worker、host runtime、host→RC 三段都使用有界低优先级日志队列；溢出丢整条并汇总，control/monitor 优先。gap 只保留有限身份，不捕获大表正文。
- 框架填充实例、runtime、session、context 和版本；initialize 日志可早于 ready，新 context 日志可早于配置 commit。旧 runtime 对象调用被拒绝，迟到日志不归入新实例。
- 新增 table_output_v1 能力协商；RC/SDK 同步 0.18.0，schema 3 和协议版本不变。新接口遇不支持能力明确提示升级，普通 JSON 接口照旧。

## 日志渲染与查询

使用现有后台 formatter 和只绘制可见行机制，每记录不新增 widget。默认展开，多表纵向排列，有标题、列标题、简洁分隔线。空表显示“无记录”，保留 0、false、null、空字符串。

前 N-1 列完整单行放下时列式，末列允许换行；非末列过长/含换行或窗口过窄时整表纵向。全文不截断，不做多列同时折行。宽度按终端格宽，文字映射按字符偏移。

整条复制原 JSON；拖选复制与窗口无关的逻辑文字，边框、填充、重复装饰标签和软换行不入剪贴板。覆盖跨单元格、跨表、跨日志正反选择。锚点按真实文字映射恢复，处理表头/装饰/续行，保留 revision 淘汰与历史机制。无效展示元数据回退已有纯文本，不改变业务回执。

| 入口 | 表格 |
| --- | --- |
| connector list | 名称、来源、实例、有效性、配置路径、错误 |
| policy list | 来源 ID、类型、状态、控制模式、有效性、详情（目录与错误） |
| status | 顶层及 runtime、connector、recording、direct 等分组字段表 |
| connector status | 连接字段表和逐实例适配表 |
| record status | 状态、会话/路径、积压、统计分组表 |
| 帮助目录/参数 | 共用排版，保留 HelpDocument、纯文本及复制语义 |

状态不省略字段：嵌套对象按完整路径展开，数组和空容器保留完整 JSON。

## 阶段与验证

1. SDK 契约/三段低优先级日志/RC 接收与 effect 展示时点，增量测试和独立审查。
2. 日志渲染/查询适配/帮助复用/选择锚点，真实 Textual 与 PTY 测试、独立审查。
3. example 的 table-stats 命令和默认关闭 params.table_logs 演示、文档与跨实例/跨容器验收，整体审查和完整接口回归。

SDK 验证普通字典、原 result、快照隔离、空表/多表/类型/非有限数/行宽/消息限制。链路验证三回调、初始化早于 ready、重初始化早于 commit、旧 context、重复来源身份、effect 成功与失败。三个出口分别阻塞和积压，验证非阻塞、控制优先和 gap 不保留 payload。

UI 验证中文/长路径/多行/宽窄切换/锚点/跨记录正反拖选/原 JSON 复制/旧 revision/输入焦点及补全。增量通过后执行 `python3 scripts/test_scope.py --interface`；隔离 Docker example 和 example 自链验收后清理。缺环境和跳过明确记为未验证，不启动真实模型或硬件。

更新策略开发、命令、验证文档，解释接口、复制、窄屏和尽力投递语义。

## 后续用户指令：只做定向验收

2026-11-11，用户在最终修复阶段明确要求：“这个修复结束后不要跑全量验收，定向验收即可，没有我的允许以后禁止跑全量验收”。本条覆盖上文完整接口回归和整套 Docker smoke 的本轮实施安排。修复后仅运行直接相关的定向测试与独立复审；未来全量验收必须获得用户对当轮的明确允许，规则已写入项目 `AGENTS.md`。此前已完成的全量结果仅代表其当时源码，中断运行不计通过。
