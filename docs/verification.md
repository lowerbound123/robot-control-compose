# Schema 3 动态策略容器验收

当前命令树与 configure 配置契约见 [统一命令树](commands.md)；下文按时间记录的旧 set/show/switch 等拼写只代表当时验收接口，现已移除。当前默认只做改动相关的定向验收；完整接口回归、整套 Docker smoke 等全量验收须获得用户对本轮的明确允许，不能因公共契约变化或阶段结束自动执行。跳过不计通过。

## Record Auto 成功后自动换段（2026-11-14）

新增本次运行有效、默认关闭的 `record auto [on|off]`。开启确保录制，关闭保留当前会话；手动 stop 保留 auto，但使旧操作／待处理换段失效。成功的 reset、姿态恢复、策略切换／链 apply、reload／reinitialize、配置重置及冷重试后台串行换段；原操作回执不等待，失败或取消不触发，resume／return 的内部 reset 不触发。connector 原有边界共用转移锁并去重。

查询及录制结果增加 auto、auto_pending、auto_error，状态栏在录制状态旁显示 recording_auto: on/off；MCP record 增加 auto_on／auto_off／auto_toggle，无需 take，控制代次检查保留。自动任务最多 256 项，意图代次阻止手动 stop／退出后的迟到重开；较旧成功回调不能清除较新错误。正在停止时的显式 auto on 等待新录制就绪，auto off 不撤销该明确启动意图，手动 stop 和退出可以取消。

最终定向验证 **168 passed，50.93 秒**，退出码 0，日志 `outputs/record-auto-20261114/final.log`。包含命令语法／补全、状态栏宽窄布局及本地只读快照、结构化 MCP、原录制行为、reduce 结果兼容、example 与自链的实际配置／生命周期效果、connector 正常与失败清理、停止中启用、旧操作失效、有界队列、错误顺序以及退出晚到回调。新增边界均有定向 red/green 证据，同目录保留初始工作区 diff 与文件哈希。

没有运行完整接口回归、全套 Docker、业务模型、真机或新的性能 benchmark；此前 MKV、CSV writer、reduce 核心和 benchmark 源码与本轮开始时的哈希一致。此前未提交改动保留，本轮没有提交、推送或服务器部署。

## 五秒无损 MKV、异步写入与 reduce 迁移（2026-11-14）

录制改用 FFV1 level 3 / bgr0 无损 RGB，30 FPS 主视频每 150 帧封口，短尾段按实际样本数结束。各路视频线程独立负责编码、轮换、同步和完成索引；视频管道、封口和索引 fsync 均不阻塞文本线程。CSV 与状态逐样本 flush/fsync 顺序保持。新格式的 sample_commit 只证明文本提交和视频受理，视频持久完成以 video_segments.jsonl 为准；策略实际图像通过采样帧复用或辅助无损流保存。

reduce 兼容旧 PNG 和新 MKV，按文件锁跳过活动 writer，允许处理异常结束的静态目录。有效 head 首帧逐像素校验；全部输出验证、发布、同步后删除对应原始片段，未封口素材保留。删除中途取消可凭完整回执与输出哈希安全续做，损坏或不完整回执保留剩余素材。

最终定向选择 **136 passed、5 subtests passed，35.04 秒**，退出码 0。日志：`outputs/recording-mkv-20261114/verified-tests.log`。覆盖奇数尺寸 RGB 逐像素一致、150 帧／五秒、短尾段、三路对齐、黑帧／保持占位、实际策略帧身份、辅助流、视频管道和索引同步阻塞、额度、编码器崩溃、磁盘错误、强杀／停止期限、信号退出、四份 CSV、reduce 新旧格式／锁／取消恢复，以及 example 和 example 自链录制启停／退出。没有运行完整接口回归、Docker 全套、业务模型或真机验收。

最终 benchmark 证据为 `outputs/recording-mkv-20261114/bench-verified/`：`report.md`、`summary.json`、`comparison.json`、`environment.json`、`audit.json`，附冻结源码、逐轮原始测量和完整录制素材。其录制源码及 benchmark 脚本 SHA256 与交付工作区一致。之前试跑目录仅为排查过程证据，不用于以下成绩。

Apple M1 Max／10 核，本机项目所在 APFS 磁盘，Python 3.10、NumPy 1.26.4、Pillow 12.3.0、FFmpeg 9.0.2。三路 640×480，相同确定性输入、文本负载及 512 MiB／64 MiB 额度；每样本一次策略观察，含少量策略专用图像。吞吐测试 450 样本，三轮交错比较 PNG 基线 6003d3a 和新 MKV，计时包括停止排空和尾段封口，随后完整解码核对全部有效像素哈希和 CSV 内容／控制关联。

| 三轮均值 | PNG | MKV |
| --- | ---: | ---: |
| 总耗时 | 8.523 s | 5.936 s |
| 吞吐 | 52.80 样本/s | 75.82 样本/s |
| 文件数 | 2727 | 24 |
| Python writer fsync 调用 | 9605 | 2863 |
| CPU 总时间 | 29.07 s | 33.97 s |
| 进程树峰值 RSS | 171.93 MiB | 361.21 MiB |

吞吐提升 **43.6%**，耗时下降 **30.4%**，文件数下降 **99.1%**，Python writer 同步调用下降 **70.2%**；FFmpeg 内部同步调用不计入该计数。CPU 与内存增加；全噪声实时场景的 MKV 文件字节数比 PNG 增加约 5.5%，文件创建／同步减少不等于所有画面都节省字节。不能据此推断所有部署都会同幅度提速。

移动加噪声和全噪声各运行 60 秒、30 Hz，两种实现均完整保存每轮 1800 个文本样本和 5400 张有效相机图像，所有已接受图像哈希一致、队列排空，无强制退出。策略观察拒绝次数分别为：移动 PNG 12／MKV 1，全噪声 PNG 8／MKV 1，首次发生在启动约 0.10–0.11 秒。保留原有 64 MiB 附加额度，没有通过调低分辨率／帧率、增加预算或减少文本同步改变比较条件。由于观察拒绝，这四轮完整性结果均记为失败；主采样完整不等于策略观察零丢失。本轮没有部署服务器，结果仅代表本机。

## record reduce 移除录制状态限制（2026-11-13，历史行为）

本节记录当时验证；活动会话处理方式已由 2026-11-14 的会话锁协议替代，当前实现跳过活动 writer。

按用户要求移除共享转换实现的停止状态检查。TUI 的指定目录／`all` 与离线入口均处理正在录制、异常结束以及缺少或不完整 `session.jsonl` 的目录，原会话日志不改写。帮助和补全已同步。reduce 不停止 writer；转换期间新增帧可能未入视频便被删除，并发写入可能失败或重新创建图像目录，详见[录制说明](recording.md)。

定向验证 **69 passed、5 subtests passed，11.26 秒**，退出码 0；日志 `/private/tmp/record-reduce-unrestricted-verified.log`。范围为 `test_recording_videos.py`、`test_record_reduce.py`、`test_completion_engine.py` 和内置帮助完整性用例，覆盖真实 FFmpeg、五种会话元数据状态、仍处于 recording 的真实 writer、指定目录／all、example 与自链运行、重复转换、损坏图像保留和清理。活动录制用例降低测试采样频率以验证状态准入，不将其视作并发删除无损保证。

新增元数据用例在旧实现上先失败。初次运行有 5 项控制器初始化被沙箱 IPC 权限阻断；开放本机 IPC 后发现子进程源码路径未设置，补齐 `PYTHONPATH` 后上述定向选择全部通过。没有运行全量验收、Docker、真实模型或机器人，没有转换服务器真实录制或部署远程源码。

## record reduce 与压缩默认值初版（2026-11-13）

新增 `record reduce <日志文件夹名|all>`，后台处理已正常停止的录制，命令、补全和离线入口共用原有转换规则。默认改为 H.264 CRF 28 / medium，保留分辨率、30 FPS 和无损 head 首帧；视频校验通过后才删除原始图像目录。

最终定向验证 **164 passed，48.69 秒**，退出码 0；日志 `/private/tmp/record-reduce-final.log`。覆盖临时录制的真实 FFmpeg 编码、指定目录/all、进行中与重复任务跳过、失败保留、部分失败汇总、目录与符号链接边界、补全、example 与自链运行不受影响、MCP 权限和 RC 退出清理。固定临时图像样本的新默认视频小于旧 CRF 18 / fast；不将该样本推断为实际录制的固定压缩比。

RC Dockerfile 增加 FFmpeg，尚未构建镜像。没有运行全量验收、clip 策略测试、真实模型或机器人，没有处理用户的真实日志，没有修改服务器运行状态。

## RC 调用调度与 history 推理分离（2026-11-13）

所有 policy 移除固定 sync 和固定调用间隔的限制，合法模式和正整数仍由 SDK/RC 校验。仅变更 RC mode/call_every_actions 保留动作缓存与 context。三个 history 来源按输入剩余动作数量决定透传或推理，pipelined 只控制接近点融合；RC 在外部 async 推理期间继续消费，并在收包时裁掉已消费前缀。

定向验证 **477 passed、3 skipped，79.54 秒**，退出码 0；日志 `/private/tmp/policy-scheduling-final.log`。覆盖配置解析、运行中模式切换、迟到暂停、原 history/LIFT2 请求与历史采样、RC 发布与前缀保护、SDK 标记传输、example 和 example 自链。三项跳过均因本机缺少 Torch，不计通过。

硬裁剪保留在 clip policy 内，仅保证返回 chunk 内限幅；RC 只对标记进行裁剪和消费计数。按用户要求没有新增或执行 clip 策略测试，也未导入其源码；仅做源码与配置静态核查。11 个 manifest 模板的执行/参数默认值未改变，22 个本地实际配置文件保持 Git 忽略。没有运行全量验收、Docker、真实模型或机器人验证，没有操作服务器运行状态。

## LIFT2 独立 connector（2026-11-12）

新增 ROS 1 Noetic / CPython 3.10 的 `lift2` 来源，支持双臂及夹爪 qpos、关节/末端状态和三路 RGB。
包含 epos 的整条请求在发布前失败，使用现有 RC 发布故障路径，不增加消费计数。
RC、policy 和两个 SDK 的既有实现文件与基线 `fdc29f1` 字节一致；现有生产代码仅扩展 setup connector 来源白名单。
配置与限制见 [LIFT2 使用说明](lift2.md)。

| 验证范围 | 最终结果 | 证据 |
| --- | --- | --- |
| 新适配器/setup 及直接相关 connector 定向回归 | **75 passed，5.81 秒**，退出码 0 | `/tmp/rc-lift2-targeted.log` |
| Linux amd64 独立 runtime / simulator 镜像构建 | 两个 target 构建成功；runtime Python **3.10.22**，约 **280 MB** | `/tmp/rc-lift2-evidence/build.log` |
| 真实 ROS 1 原始与压缩图像传输 | **2 组通过**：消息 MD5、状态、逐像素 RGB、只读租约、FIFO、epos 零发布、反馈/图像过期、正常退出 | `/tmp/rc-lift2-evidence/evidence/transport.json` |
| RC example、自链与 epos 故障 | **3 组通过**：qpos、policy 只读图像、预览数据、录制、mock↔lift2 切换保留 policy 容器/PID、epos 不消费且停止新增动作 | `/tmp/rc-lift2-evidence/evidence/controller.json` |
| 清理与来源核对 | RC 正常退出、policy 图像池归零；专用容器/卷已移除；三份厂商消息 SHA256 与原镜像一致 | `/tmp/rc-lift2-evidence/evidence/cleanup.json`、`/tmp/rc-lift2-evidence/source-check.json` |

Docker 验收由 `tests/lift2_docker_acceptance.py` 在 `pjlab@10.6.3.69` 的专用临时目录执行，
ROS master 和模拟器使用 `--network none`，仅测试 connector 加入其网络命名空间。
RC 保持无 ROS 的镜像，example 在独立 Python 3.14 容器运行。实际连接器源码和本地最终源码核对哈希，
镜像依赖/体积证据为 `/tmp/rc-lift2-evidence/image-source.jsonl`。

定向测试先后覆盖并修复：初始化阻塞未受期限约束、末端回调改写历史、注销失败中断清理、
命令 TCPROS 握手晚于状态反馈、网络配置类型和 float32 序列化范围。
构建阶段补齐纯 Python ROS 依赖，并排除 macOS 传输附带的 AppleDouble 文件。
测试脚本中图像视图残留、单实例/链 worker 访问差异及累计 consumed 断言均已修正，
上述结果为修正后最终完整执行该定向选择的结果，早期失败不计通过。

本轮按用户要求单智能体实施与自审，没有运行完整接口回归、整套 Docker smoke、真实模型或硬件验收，
没有启动厂商驱动或向真机发送动作。新镜像保留在远端供后续部署；原推理项目和原镜像未修改，未自动提交或推送。

## 输入框上方状态栏表格化（2026-11-12）

固定状态栏改为非聚焦的字段/值表格，保留原 13 项白名单、行优先顺序、空值占位、False/0、请求摘要和完整错误/连接路径。实际内容宽度达到 114/76 格时分别显示三组/两组，其余显示一组；小于 12 格时去掉装饰框线与内边距。字段和值完整折行，未增加 SDK、协议或控制器状态接口。

状态区高度最多约为窗口的三分之一，并扣除输入、候选、policy manager、monitor 和 camera 的必要空间；超出内容内部滚动。日志外框至少保留四行，含两行实际内容。格式化后的可见行和宽度不变时不重建表格，窗口缩放后重排，状态变化保持滚动位置和输入焦点。另以两行分支修复极窄日志空分隔行缺少样式导致的 Textual 过滤异常，原日志选择、复制和命中语义不变。

- 状态栏、输入辅助、请求摘要、日志表格和相机/监控的定向回归：**59 passed，45.84 秒**，退出码 0；9 条告警均为既有 Pillow/Sixel `getdata()` 弃用提示。确切命令见 `/private/tmp/rc019-statusbar-table-report.md`，日志 `/private/tmp/rc019-statusbar-table-final-tests.log`。
- 独立审查：**Spec / Quality PASS**，无新增 actionable finding；另复核八个宽度边界、刷新后的滚动位置，并重跑三个直接相关用例，**3 passed，2.12 秒**。报告 `/private/tmp/rc019-statusbar-table-review.md`，八文件冻结清单 `/private/tmp/rc019-statusbar-table-files.json`。
- 实际 Textual 导出覆盖 120、80、50、28 列及 28×16 补全展开场景。50×24 保留 11 行实际日志；28×16 展开补全仍保留输入、候选与两行日志。原始 SVG/布局 JSON 位于 `/private/tmp/rc019-statusbar-*`，已目视检查宽屏与窄屏滚动底部；临时 CJK 格宽校正 PNG 位于 `/private/tmp/rc019-statusbar-preview-pngs/`，未修改产品或第三方库，不等同原生桌面截图。

按用户要求仅运行定向验收，未运行完整测试范围、Docker、模型或硬件；初次沙箱内 IPC 权限失败不计通过。基线为 `5d181d7`，工作仍在 `codex/tui-statusbar`；本次显示调整未自动提交或推送，私有配置保持原样。

## 0.18 TUI 通用表格与 policy 输出（2026-11-11 至 2026-11-12）

RC 与 policy SDK 更新至 0.18.0，新增 `table_output_v1` 能力，manifest schema 3 与既有协议版本保持不变。SDK 的标准库表格层提供 `Table`、`TableOutput` 和 `runtime.log_table()`。仅显式包装使用表格，普通字典、打印与业务 JSON 保持原有语义；effect 命令在最终成功后显示表格，失败不发布成功展示。

日志区支持多表、中文格宽、末列换行及整表纵向回退。`connector list`、`policy list`、`status`、`connector status`、`record status` 使用明确适配器，帮助目录与参数共用布局。未知嵌套字段、数组、空容器、长路径、0、false、null 和空字符串均保留。整条复制保持原始业务 JSON 的既有公开副本；拖选使用宽度无关的逻辑文字，帮助继续复制完整帮助文字。此前固定状态栏不变。

三个传输出口分别使用有界低优先级日志队列，控制回执与 monitor 优先；`log_table()` 的布尔值只表示本级入队，溢出整条丢弃并汇总，不能当作持久化确认。初始化早于 ready、候选 context 早于配置提交、重复来源实例、旧 runtime 拒绝与迟到身份校验均有真实 worker/自链测试。example 提供 `table-stats`，`params.table_logs` 默认关闭；只更新模板，不自动覆盖私有清单。

实施与独立审查均使用 gpt-6-astra / max，按 SDK/传输、TUI/查询、example/文档三个阶段交接。前两阶段审查修复了大表脱敏成本、接收锁占用、失败 effect 持有展示数据及字段展开后的脱敏问题。整体审查另外发现录制写入器对连续 JSON 单元格递归过深，以及表格投影丢失原持久化敏感字段语义；定向验收覆盖这些问题、纯文本 JSON 探测、转义凭证传播、无效复制元数据回退及实际落盘。

最终修复后的定向证据如下，新增文件的 49 项包含在 230 项中，不重复累加：

| 范围 | 结果 | 证据 |
| --- | --- | --- |
| 录制、脱敏、表格输出与控制器定向回归 | **230 passed**，30.17 秒，无跳过、无告警，退出码 0 | `/private/tmp/rc018-final-fix3-targeted-regression.log` |
| 表格持久化专项 | **49 passed**，2.66 秒；含实际 writer idle/active、基础/events/policy 文件、持续采样与正常停止 | `/private/tmp/rc018-final-fix3-tests-green.log` |
| 普通 JSON/文本行为对照 | **1792 组，0 差异** | `/private/tmp/rc018-final-fix3-text-comparison.log` |
| 整体审查的原始及残余复现 | 全部通过；大表、转义凭证、字面括号、非有限复制元数据均保留对应安全语义 | `/private/tmp/rc018-final-fix3-original-green.log`、`/private/tmp/rc018-final-fix3-residual-green.log`、`/private/tmp/rc018-final-fix3-n1-green.log` |
| 最终独立复审 | **Spec / Quality PASS**，全部发现关闭；另行复核 4 个原始案例及 6 种数字文字，无未关闭或延期项 | `/private/tmp/rc018-final-fix3-review.md` |

定向回归使用本机 `.venv/bin/python3.10`（3.10.22），确切命令与文件清单见 `/private/tmp/rc018-final-fix-report.md` 的 Round 3。复现修复前先失败，再验证实际文件；没有通过吞掉写入异常、缩短表格或降低合法行数来取得通过。

修复后的源码在定向验证与复审期间保持冻结，最终两个文件的哈希与 `/private/tmp/rc018-final-fix3-files.json` 一致。交付保留在 `codex/tui-statusbar` 的未提交工作区，保留此前状态栏、私有配置及另一项 Qwen 历史修改；没有自动暂存、提交、推送或操作远端部署。

阶段证据不累加为总用例数：SDK 最终增量 142 passed、1 skipped；TUI/查询组合 283 passed，字段脱敏修正后 56 passed；example/文档增量 159 passed。对应日志为 `/private/tmp/rc018-fix2-final.log`、`/private/tmp/rc018-task2-final-regression.log`、`/private/tmp/rc018-task2-fix1-final-green.log`、`/private/tmp/rc018-task3-incremental.log`。

真实 Textual 测试覆盖宽窄切换、滚动锚点、跨单元格/表/日志的正反向拖选、原 JSON 复制、异步旧 revision 淘汰、输入焦点与补全，相关 PTY 回归同时通过。原始 SVG、逻辑映射 JSON 位于 `/private/tmp/rc018-table-*`；120、50、24 列及窄屏底部预览已目视检查，PNG 位于 `/private/tmp/rc018-table-previews/`。预览仅对 Rich SVG 的中文格宽导出问题做临时定位校正，未修改产品或第三方包，也不等同原生桌面终端的人工验收。

实现采用两项明确边界：每张表至少一列、每次输出至少一张表，空结果使用零行表并显示“无记录”；主动表格日志按尽力投递，而命令结果仍走控制回执通道。既有敏感字段规则继续生效，完整展示不意味着取消脱敏。

用户在最终修复期间明确要求本轮只做定向验收，并禁止以后未经明确允许运行全量验收。该要求已写入 `AGENTS.md`，覆盖本计划原定的最终完整接口回归和整套 Docker smoke。本轮没有执行跨容器 table smoke；Docker 仅完成环境/依赖预检查，不计为跨容器验收。ROS、真实模型、机器人硬件未运行。

修复前的完整接口记录为 2167 passed、78 skipped、17 warnings、732.02 秒，保存在 `/private/tmp/rc018-interface-prereview.log`，只能代表当时源码。后续一轮完整回归因复审发现剩余边界被主动中断，日志 `/private/tmp/rc018-interface-interrupted-string-boundary.log`；KeyboardInterrupt 和中断引起的清理断言不计为完成或通过，之后已确认无 SDK/录制子进程残留。最终修复后的证据以本节的定向验收和复审为准，不将旧全量结果替代最终验证。

## TUI 固定状态栏精简（2026-11-11）

本次只调整 RC 内部显示，未改变 `status()`、MCP 或 SDK 公共返回结构。常驻栏使用固定 13 项白名单；录制拆成状态与错误，连接只显示当前有效绑定的来源和原始配置路径。完整诊断仍通过 `status`、`connector status`、`record status` 查询，三条帮助和命令文档已同步。

连接元数据在 RC → connector 锁顺序下读取，只访问内存。测试覆盖提交前保留旧绑定、断开后清空、连接失败与显式重试、连接成功但 policy 适配失败、缺少 spec，以及修改磁盘 profile 不改变当前显示。真实 Textual 用例覆盖空值、False/0、请求摘要更新和失效、中文、长路径、完整错误及宽窄窗口。

高度不足时状态栏内部滚动，输入与候选保持可见；恢复宽窗口后自然展开。独立审查发现并修复了纯换行错误丢失占位和滚动容器抢输入焦点两项问题。最终容器不加入 Tab 焦点链，点击状态文字后仍可输入，鼠标滚动继续可用。

| 范围 | 结果 | 证据 |
| --- | --- | --- |
| TUI、状态查询、connector、帮助及 PTY 组合回归 | **239 passed**，114.14 秒 | `/private/tmp/rc-statusbar-green-combined-final.log` |
| 最后焦点修正后的状态栏、输入辅助和 PTY 定向回归 | **34 passed**，32.34 秒 | `/private/tmp/rc-statusbar-green-focus-regression.log` |
| 最终默认 example 与 example 自链回归 | **82 passed、1 skipped**，85.24 秒，退出码 0 | `/private/tmp/rc-statusbar-delivery-scope.log` |
| 独立最终复审 | Spec / Quality 均通过，无未关闭发现；**6 passed** 定向复验，14 文件哈希一致 | `/private/tmp/rc-statusbar-review.md` |

组合回归之后的产品修改仅为状态滚动容器增加 `can_focus=False`，随后执行表中定向回归、独立复验和默认范围回归。使用现有 `.venv/bin/python` 3.10.22；tracked diff 和新增文件的空白检查通过。实现报告与源码清单分别为 `/private/tmp/rc-statusbar-report.md`、`/private/tmp/rc-statusbar-delivery-hashes.json`。

Textual 导出覆盖 120 列、50 列和 28×16 极小终端，包括候选展开、滚动至路径末尾、收回及恢复宽窗口。原始 SVG、布局 JSON 和按终端格宽校正的预览位于 `/private/tmp/rc-statusbar-*`；已目视检查宽屏与极小终端预览。校正仅用于规避现有 Rich SVG 中文 `textLength` 导出问题，没有修改产品或第三方包，也不代替原生桌面终端的人工验收。

唯一跳过项需要 ROS 2 和已构建消息包，不计通过。本轮没有运行 Docker 部署、ROS、真实模型或机器人硬件，没有修改私有配置或操作远端部署；工作保留于 `codex/tui-statusbar`，未自动提交或推送。

## 0.17 连接命名与统一帮助（2026-11-11）

RC 与 policy SDK 更新至 0.17.0，保留 schema 3。裸 `camera` 等价 `camera toggle`；机器人连接命令、MCP 工具、配置、状态与图像代次、能力协商和录制字段统一使用 connector。旧命令及配置直接报错，没有别名或自动迁移。Profile 的 `source` 和 `connector` 分别保留来源、实例含义。

帮助使用静态 `help_details`、可序列化 `HelpDocument` 和完整纯文本投影。TUI 在现有日志区排版，headless 保留纯文本，MCP 帮助提供文本与结构化数据。未加载来源和原生 chain 的帮助不导入模型；重复策略按确切实例填充示例。普通共享 JSON 即使包含 `help`、`help_document` 键也不会被当成帮助。完整内置路径如 `help policy switch`、`help mcp take` 可达，`--reset` 只展示一个可选 flag。

最终验证使用本机 `.venv/bin/python` 3.10.22；默认系统 Python 3.9 不参与本次测试。独立全分支审查发现的两个功能问题和一个重复参数问题均已修复，修复范围复审通过。测试期间通过源码哈希确认最终实现保持冻结。

| 范围 | 最终结果 | 证据 |
| --- | --- | --- |
| 完整接口回归 | **1960 passed、78 skipped**，退出码 0，678.88 秒 | `/private/tmp/rc017-postreview-interface.log` |
| WebUI | **175 passed**；生产构建通过 | `/private/tmp/rc017-webui-unit-final.log`、`/private/tmp/rc017-webui-build.log` |
| 真实隔离 Docker example 与自链 | **9 组检查通过**；含 connector 切换、实例身份、MCP、静态帮助及 HTTP 帮助双投影 | `/private/tmp/rc017-postreview-docker/result.json` |
| Docker 清理 | 本次项目 `rc-077786c866b3` 的容器、卷均为空 | `/private/tmp/rc017-postreview-docker/cleanup.json` |
| 帮助内容与入口增量 | 267 passed；审查修复相关 77 passed | `/private/tmp/rc017-task2-final.log`、`/private/tmp/rc017-task2-review-green.log` |
| 日志与实际 Textual 交互 | 110 passed，含中文、窄屏、滚动、正反向拖选、复制、缩放与异步旧排版丢弃 | `/private/tmp/rc017-render-final-regression.log` |
| 独立审查与修复复审 | 已确认缺陷全部关闭 | `/private/tmp/rc017-final-review.md`、`/private/tmp/rc017-fix-review.md` |

完整回归保留 14 条告警：13 条现有 Pillow/Sixel 弃用告警，以及 policy monitor 生命周期测试中的一次 `multiprocessing.resource_tracker` 重启告警。首次最终回归的两处旧帮助返回结构断言已按新契约修正，原有运动、图像和代次断言保留；上述最终全套为修复后重新完整运行的结果。

原始 Textual SVG 截图保存在 `/private/tmp/rc017-help-*.svg`。Rich 15 的 SVG 导出按字符数设置 `textLength`，会压缩中文；实际终端 Strip 已验证双宽字符及列对齐。`*-cells.svg` 与对应 PNG 仅在临时预览中按实际终端格宽逐字定位，用于目视核验，未修改产品或第三方包。原图、校正预览和映射测试证据均保留。

78 项跳过涉及未安装的可选策略依赖、ROS/GPU/模型及未启用的专用镜像等环境条件，不计通过。本次没有运行真实 ROS 驱动、真实模型供应商、GPU 模型或机器人硬件，没有操作远端部署，也没有自动修改私有配置。


## 0.16 集成：TUI、热切换与独立 connector（2026-11-07）

本次整合全部九个工作树的历史与四组未提交工作：TUI 命令／policy 配置／输入辅助、backend 热切换、独立 connector、Compose 快捷脚本。RC 与 policy SDK 为 0.16.0，connector SDK 为 0.1.0，manifest 保持 schema 3。

命名 backend profile 保存 `{connector, source, config}`。RC 不加载 ROS；切换前冻结配置、停止新增动作、收齐旧回执和读者释放证明，确认旧 connector 退出后创建新实例。正常 policy 容器与 PID 保留，按各实例 `execution.on_backend_switch` 执行 reset 或原进程重初始化。普通 policy 生命周期不重建 connector；失败需要明确手动恢复，强制清理仍保留非零退出事实。

最终生产代码基线为 `a9f11d6`。验证分别执行，未把跳过或旧分支的结果计入通过：

| 范围 | 最终结果 | 证据 |
| --- | --- | --- |
| `python3 scripts/test_scope.py --interface` | **1863 passed、78 skipped**，679.34 秒；13 条 Pillow 弃用警告、1 条 resource_tracker 重启告警 | `/tmp/rc-all-worktrees-interface-complete.log` |
| 真实 Docker example／自链／MCP | **9/9 组通过**，harness 退出 0；RC Python 3.10.12 无 ROS、example 独立 Python 3.14 | `/tmp/rc-all-worktrees-docker-final/` |
| Linux/Humble 真实 ROS 与 connector | **61 passed、0 skipped**，36.85 秒；源码只读、容器断网、使用独立 ROS simulator | `/tmp/rc-all-worktrees-linux-ros.log` |
| WebUI 单元测试、类型检查及构建 | **175 passed**，类型检查与生产构建通过 | `/tmp/rc-all-worktrees-webui-unit.log`、`/tmp/rc-all-worktrees-webui-build.log` |
| 完整 Chromium 浏览器验收 | **43 passed**，5.6 分钟；保持原断言和超时 | `/tmp/rc-all-worktrees-webui-e2e-final.log` |

Docker 验收包含 209 个普通操作检查点，均保持 connector CID/runtime 不变；显式 backend 切换才替换两者，同时核对各 policy 的容器、host、runtime、PID、context、配置及录制分段。模拟 create-result 丢失的强制清理非零状态仍按预期记录。专属部署 `rc-b820aa1b3404` 的容器、卷和网络均已清空，见 `cleanup.json`。ROS 的 61 项包含全部八项真实 ROS 流程，覆盖跨域切换、慢推理、运动中退出、位姿保存恢复、相机与反馈过期，容器已自动移除。

独立 Astra 审查复核了集成及两项新增修复：connector 启动握手、状态、图像、释放和分段收发共用绝对期限；无状态 JSON MCP 不再建立无用 GET SSE。后者在 Bearer、Host、Origin 校验后返回 405，保留 POST 与相机／状态流；新增 HTTP 专项及既有流式回归 34 项通过，独立复核 18 项通过。真实 Chromium 连接预算探针确认五条长连接导致短请求串行，移除无用 MCP 长连接后恢复两个短请求位置；未增加测试超时或引入 manifest 缓存。

中间失败记录保留：中断轮未完成；接口首轮四个 fixture 错误来自历史策略仍导入旧 MockRobot，迁移共享 connector helper 后专项四项通过，再运行最终完整套件。浏览器首次因默认缓存缺二进制而未执行产品断言；随后并行轮 37/43、隔离轮 40/43，依据 trace 和连接预算探针修复 MCP 资源问题后完整 43/43。日志保留在 `/tmp/rc-all-worktrees-interface-final.log`、`/tmp/rc-all-worktrees-webui-e2e{-verified,-isolated}.log`，不作为通过结果。

未运行真实模型、GPU 或厂商硬件。ROS simulator、mock、跳过项及本机性能均不能替代真机验收。所有源码、测试和文档提交均排除 `.codex`、私有配置与运行产物。

## Compose 快捷入口（2026-11-07）

新增 `scripts/up.sh`、`build.sh`、`down.sh`、`attach.sh`，保留原 `compose.sh`。公共生命周期、策略与 SDK 契约不变。

- 脚本、setup、配置导入和退出回归：**42 passed**，其中新脚本 **19 项**。覆盖顺序合并、路径空格、RC 别名、真实 Compose 服务解析、独立策略环境、歧义/无构建定义拒绝，以及退出码传递。
- `python3 scripts/test_scope.py`：**78 passed、1 skipped**；宿主 ROS 环境缺失，跳过不计通过。沙箱首次运行禁止 POSIX 共享内存，相关失败运行不计通过；获得测试权限后完整重跑通过。
- `python3 tests/example_docker_smoke.py --evidence-dir /tmp/rc-compose-shortcuts-example`：7 组真实 Docker 检查通过，覆盖 example 自链、重载、共享图像、MCP 与退出证明；测试部署容器和卷均已清理。
- 独立临时部署执行 `./setup.sh --policies example` 和 `./scripts/build.sh example` 成功；新 `up.sh` 启动 RC、anchor、附加容器及两个 example，随后无参数 `down.sh` 成功回收全部 5 个容器，无需再次提供覆盖文件。使用 Compose 5.5.0，mock 机器人；未连接硬件。
- Shell/Python 语法检查、`git diff --check` 通过。单智能体实施与自审。

证据：`/tmp/rc-compose-shortcuts-targeted-full.log`、`/tmp/rc-compose-shortcuts-scope-full.log`、`/tmp/rc-compose-shortcuts-example/`、`/tmp/rc-compose-shortcuts-live.log`、`/tmp/rc-compose-shortcuts-live.json`。

## WebUI 深浅主题切换（2026-11-06）

顶部太阳／月亮按钮切换深浅主题，默认沿用浅色并记住浏览器偏好；全页面及 Canvas 配色适配，主题切换保持录像、位姿历史和 AI 请求连续。

- `npm test`：**166 passed**；`npm run build` 与 `git diff --check` 通过。
- 完整 Playwright：**43 passed**，覆盖原有功能和新增的主题切换、Canvas 实际颜色、首屏恢复、刷新/跨标签同步、存储失败、AI 请求期间切换与 320～1440px 布局。
- 最后视觉检查加深了深色日志底板，避免后方相机文字干扰阅读；修正后主题专项 **4 passed**，构建再次通过，最终截图位于 `/tmp/rc-theme-visual-final/`。
- 生产构建在完全禁用 `localStorage` getter 后仍可双向切换，页面错误为空。开发服务器的存储依赖通过仅对主题键注入读写故障单独验证。
- 首次完整回归中 mock RC 在第 4 项后阻塞，健康接口也超时，导致 8 项连锁失败；中断并确认测试进程退出后，在禁止复用服务的新隔离环境中完整重跑通过。未将该次失败计为通过，未在本次主题改动中修改 RC。

证据：`/tmp/rc-theme-vitest.log`、`/tmp/rc-theme-build.log`、`/tmp/rc-theme-e2e-clean.log`、`/tmp/rc-theme-production-check.log`；截图在 `/tmp/rc-theme-e2e-clean/`，首次阻塞日志保留于 `/tmp/rc-theme-e2e-final.log`。单智能体实施与自审；本次仅修改 WebUI，未执行 Docker、ROS 或硬件验收。

## WebUI 位姿曲线验收（2026-11-06）

新增关节与末端两页、每臂 7 张自适应正方形曲线、60 秒独立视窗与全网页会话历史。RC 以共享采样器提供 50 Hz 目标状态 SSE；原 MCP state、SDK 与 policy 契约不变。多页面共用网络接收，各自保留历史和录像。

- `python3 scripts/test_scope.py`：**78 passed、1 skipped**（ROS 环境缺失，跳过不计通过），包括 example 和 example 自链。
- 状态流、原图像流、MCP、本地调用和 Web 控制代次专项：**42 passed、3 skipped**；3 项 Docker 代理检查未运行，本机 Docker 守护进程不可用。覆盖鉴权、共享采样、慢读者、反馈错误与恢复、退出唤醒、统一期限与清理失败非零退出。
- WebUI `npm test`：**166 passed**；`npm run build` 通过。
- WebUI `npm run test:e2e`：**39 passed**。Chromium 156、本地 Vite + mock RC + 真实 example worker；检查 320/390/768/1440px 布局、独立滚轮与跟随、切页、真实服务端断流恢复、三页面共享接收及接收页面关闭后的接替，原录像、AI、直接命令和控制权流程均通过。
- 真实 HTTP → Worker → 页面持续接收 20.009 秒、903 个有效快照，**45.13 Hz**，达到至少 30 Hz 的目标。此为 mock 快照接收性能，不代表硬件传感器帧率。
- 分块历史性能样本：3,001 条与 360,001 条记录下，14 图当前窗口提取和降采样中位耗时分别 **1.406ms / 1.368ms**，输出均为 5,070 个显示点；两小时后最早一分钟的 3,001 条原始样本仍可读取。这是数据处理基准，不是两小时浏览器或硬件耐久测试。

初次完整浏览器回归发现第四条常驻流耗尽多页面 HTTP/1 连接池，导致第二页录像不可用；已通过 Worker 内 Web Locks/BroadcastChannel 共用接收修复，并由完整套件验证。沙箱下 IPC/本地端口受限的运行不计通过；上述通过结果使用隔离测试所需权限。

证据：`/tmp/rc-pose-example.log`、`/tmp/rc-pose-backend-final.log`、`/tmp/rc-pose-vitest-final.log`、`/tmp/rc-pose-e2e-final.log`、`/tmp/rc-pose-history-benchmark.json`，截图与频率数据位于 `/tmp/rc-pose-e2e-final/`。单智能体实施与自审；未启动 Docker 部署、ROS、真实模型或硬件。

## Backend 热切换（2026-11-06）

RC/SDK 0.14，manifest 保持 schema 3。覆盖命名连接、默认原进程重初始化、可热改 reset、重复实例混合处理、多失败实例定向冷重试、MCP控制权与取消、图像代次隔离及录制分会话。操作说明见 [backend 热切换](connector-switching.md)。

- 公共接口完整回归：隔离 Linux / Python 3.10 / ROS Humble 环境运行 `python3 scripts/test_scope.py --interface`，**1387 passed、83 skipped**（525.59s）；日志 `/tmp/rc-backend-linux-final.log`。13条警告均来自既有 Pillow/Sixel 弃用提示。Python 3.10 的 Future 超时异常与 builtin TimeoutError 不同，修正了对应调度测试的捕获类型，没有修改模型调度实现。
- 宿主阶段完整回归：Python 3.11，**1583 passed、66 skipped**（601.52s），日志 `/tmp/rc-backend-interface-final.log`；最终图像/MCP补强专项 **34 passed**，日志 `/tmp/rc-backend-final-review-green.log`。
- 真实 ROS 传输专项：**2 passed**（8.34s），日志 `/tmp/rc-backend-ros.log`。隔离容器内运行两个不同 ROS 域的测试模拟器，反复切换 Mock→ROS→Mock及不同命名ROS配置；核对 policy 进程保留、reset context保留、重初始化context更新和旧ROS线程退出。未接入硬件。
- 真实 Docker 验收：`tests/example_docker_smoke.py` **8项检查通过**，RC Python 3.10.12，两个独立 example 容器 Python 3.14.9。混合reset/reinitialize保持CID、host身份和PID，版本/context按实例变化，录制自动分会话。证据 `/tmp/rc-backend-docker-final/result.json`；`cleanup.json`确认本次容器和卷已移除。既有丢失create回执故障注入触发的强制清理退出码1按预期保留，验收脚本退出0。
- WebUI：**158项单测通过、生产构建通过、Chromium 35项验收通过**（2.1m）。日志 `/tmp/rc-backend-web-unit.log`、`/tmp/rc-backend-web-build.log`、`/tmp/rc-backend-web-e2e.log`。使用专用mock服务、测试凭证和已有浏览器缓存。
- 最后对录制内部缓存补充修正，确保自动新会话独立保存globals基线，不改变policy可见接口。修正后按RC范围运行 `python3 scripts/test_scope.py`：**78 passed、1 skipped**（66.50s）；全部backend与录制相关用例：**97 passed、1 skipped**（42.52s）。日志 `/tmp/rc-backend-final-rc-scope.log`、`/tmp/rc-backend-final-recording.log`。

没有启动真实模型或厂商硬件驱动；GPU权重保留、实际模型切换表现和硬件命令中断后的保持/看门狗行为仍需现场验收。所有跳过项不计通过。旧测试镜像缺YAML依赖、浏览器初次缺运行路径/缓存及验收HTTP默认等待不足的失败尝试均不计通过；最终验证使用完整依赖环境，HTTP测试客户端等待长于服务端生命周期等待。

## Qwen3.5 视觉历史策略（2026-11-06）

新增独立 `qwen35_history`，使用现有每动作回调、消费计数和生命周期，不修改RC/SDK接口或旧Qwen。实现及部署边界见 [策略说明](../policy/qwen35_history/README.md)。

- 完整所选范围 `python3 scripts/test_scope.py --policy qwen35_history`：**120 passed、13 skipped**，55.88秒。包含example基线、同步/异步X步调度、历史、正式入口、真实本地host/worker与POSIX图像租约、任务热改和实例冷重载。宿主没有Torch/Transformers，11个CPU后端用例和1个图测试模块在这里跳过；另1项需要ROS。
- 独立Python3.11 / Torch2.10 / torchvision0.25 / Transformers5.2 CPU环境：后端、图回退、调度、配置和部署静态套件 **53 passed、1 skipped**，2.43秒。覆盖实际张量处理、三路输入、历史量化、32↔14维映射、delta/abs与quantile归一化、FP32专家投影、Euler更新、非有限图差异回退及源补丁delta-rule数值。唯一跳过项为真实NVIDIA CUDA捕获回放。
- 两个自审回归先失败再修复：checkpoint loader隐式依赖`packaging.version`导入顺序；prefix graph接受NaN参考差值。新补丁的三处CPU零张量分配改为在输入设备分配，meta设备测试先失败后通过，手算递推数值保持一致。
- 实际`dagger_clean_win`的config/train_config在本地成功反序列化，远端统计字段核对为左右各6+1维。只读取元数据，没有加载真实权重。
- 新增Python文件语法检查、来源SHA-256校验及`git diff --check`通过；RC、SDK、旧Qwen包/vendor/manifest/依赖锁的diff为空。本轮单智能体实施与自审，未构建或运行Docker、GPU模型、ROS或机器人硬件；跳过不计通过。

证据：`/tmp/qwen35-history-scope-final.log`、`/tmp/qwen35-history-cpu-final.log`、`/tmp/qwen35-history-review-red.log`、`/tmp/qwen35-history-review-green.log`、`/tmp/qwen35-history-graphs-red.log`、`/tmp/qwen35-history-graphs-green.log`。

## 轻量 connector 分离（2026-11-06）

实现和迁移方式见 [connector 说明](connectors.md)。RC 与 mock 镜像没有 ROS；ACOne 的 ROS 依赖、消息构建和 topic 配置由独立来源维护。此次为单智能体实施与自审，没有修改 policy SDK 的动作／观测形状及依赖。

- 完整接口套件：**1353 passed、64 skipped**，476.51 秒，13 条既有 Pillow 弃用警告。跳过项涉及宿主 ROS、可选模型／供应商依赖、Linux 专有行为和显式启用的 WebUI 镜像检查，不计通过。
- 最后的退出预算补丁修复了回执回调占用时额外等待 1 秒的问题，回归先复现 50 ms 预算实际耗时约 1 秒，再验证使用剩余绝对预算。补丁后完整 example 基线 **78 passed、1 skipped**，68.70 秒；connector／录制退出／动态容器专项 **63 passed**，11.66 秒。日志 `/tmp/rc-connector-example-exit-final.log`、`/tmp/rc-connector-exit-final.log`。
- Linux/Humble 的连接、FIFO、共享图像、消费确认、退出与真实 ROS 消息专项：**34 passed**，17.69 秒。其中历史 Conda ROS 用例已迁为 example＋独立 ACOne connector，覆盖慢推理 sync/async、policy 切换保留 connector及运动中退出。
- 真正独立容器的 mock example／example 自链 **7 组通过**；动态容器与宿主 down **10 组通过**。无 ROS RC → ACOne connector → ROS 模拟器 **7 组通过**，核对 RC 无法导入 rclpy、policy 重建期间 connector 身份不变及资源清理。故障注入产生的强制清理退出码 1 按预期保留。
- 自审回归覆盖关闭失败仍记录已收到的发布回执、重复回执不重复消费、FIFO 内校验错误不越过旧动作、输入图像慢读者保护、旧 RC 读者退出证明，以及 socket bind 与 listen 之间的启动竞态。

Linux / OrbStack arm64、三路 640×480、开启录制时，各测量约 30 秒。以下是实测，不能视为硬实时保证：

| RC 配置 | 实际提交 | 回执延迟 P95 | 峰值未确认请求 | 录制缺失帧／采样缺口 |
| --- | ---: | ---: | ---: | ---: |
| 30 Hz | 29.79 Hz | 2.23 ms | 1 | 0 / 0 |
| 100 Hz | 94.49 Hz | 2.56 ms | 2 | 21 / 7 |

两组录制结束后的待写字节和采样数均归零；可选诊断事件分别丢弃 23、489 条，报告保留这些计数。100 Hz 配置未达到持续精确 100 Hz，且该组合负载的录制仍有缺口；不能将配置值当作验收频率，也不能把下面 50 Hz 工作负载通过的录制验收推断为 100 Hz 保证。调整线程设置前另一次测量为 29.95／96.55 Hz、0／1 个采样缺口，保留原报告；这些不同负载时点的测量不能单独证明线程设置提高了吞吐。

录制长测暴露新增 I/O 线程下的采样尾延迟。GC 诊断未发现足以解释这些缺口的长暂停；将 RC 的理想线程切换间隔上限从默认 5 ms 降至 1 ms 后，独立对照通过，随后纳入 RC 启动。保持操作者已有的更短间隔，connector 不增加调度职责。[CPython 的线程切换间隔](https://docs.python.org/3.10/library/sys.html#sys.setswitchinterval) 只是理想时间片，系统调度与长时间原生调用仍可能超过它。

最终正式录制独立运行 60 秒：**通过原零缺口门槛**，1801 个采样、约 30.02 Hz，缺失帧／采样缺口／可选事件丢弃均为 0，4569 个唯一 PNG 像素哈希、5998 条逐臂发布事件及 60 次模型输入全部对齐；正常清理，无待写队列。证据在 `/tmp/rc-connector-record-isolated-delivery/`。

独立 ACOne ROS 录制 10 秒冒烟通过：1000 条实际发布与模拟器收到的消息逐臂一致，图像和 10 次模型输入核对通过，无采样缺口或强制清理。它只算 ROS 冒烟，不替代 60 秒持续验收；证据在 `/tmp/rc-connector-record-ros-delivery/`。

保留失败证据：调整前的 60 秒试验分别出现 3、1、1 个采样缺口，原 HEAD 对照在同环境通过；与完整接口套件同时运行时，调整后的录制仍有缺口，预览三路平均约 29.20 fps，未达 29.5 fps。独立负载通过不等于并行压力通过，也不构成硬实时保证。原始报告分别位于 `/tmp/rc-connector-record-60*/`、`/tmp/rc-connector-record-baseline/`、`/tmp/rc-connector-record-delivery/` 和 `/tmp/rc-connector-preview-delivery-600/`。

生产 WebUI、独立 mock connector 和 example 自链连续运行 600.009 秒，三路均为 640×480。Chromium 按图像中的源序号统计实际显示的新帧，未把重复旧帧计为新帧；三路均通过原有 29.5 fps 门槛，浏览器错误为 0。

| 相机 | 浏览器实际新帧率 | 延迟 P50 | 延迟 P95 | 最大延迟 |
| --- | ---: | ---: | ---: | ---: |
| 左腕 | 29.89 fps | 58.97 ms | 70.30 ms | 254.30 ms |
| 头部 | 29.89 fps | 59.97 ms | 69.97 ms | 237.97 ms |
| 右腕 | 29.89 fps | 61.30 ms | 80.30 ms | 272.30 ms |

最终独立预览原始报告和截图位于 `/tmp/rc-connector-preview-isolated-600/`；部署与统一清理记录位于 `/tmp/rc-connector-preview-isolated/`。测试夹具已从进程内 mock 改为独立 connector，输入像素实际经过两段共享内存所有权。线程设置调整前的一轮 600 秒预览也通过，三路约 29.66–29.68 fps，保留于 `/tmp/rc-connector-preview-600/`。

完整套件日志：`/tmp/rc-connector-interface-delivery.log`；Linux 专项：`/tmp/rc-connector-linux-delivery.log`。Docker 结果与清理证据：`/tmp/rc-connector-example-docker2/`、`/tmp/rc-connector-dynamic2/`、`/tmp/rc-connector-ros-reviewed/`。最终提交节奏报告：`/tmp/rc-connector-cadence-delivery/result.json`，调整前报告保留于 `/tmp/rc-connector-linux-cadence/final.json`。这些是本次本机证据，不提交运行产物。没有启动厂商驱动、真实模型供应商或机器人硬件。

## 按改动选择测试

在已安装 RC 与测试依赖的 Python 环境中，从仓库根目录运行。入口会为测试及其子进程设置当前仓库的源码路径；不安装依赖、不构建模型镜像。范围由改动的实际语义决定，不用文件名推断接口是否改变。

| 改动 | 命令 | 验证范围 |
| --- | --- | --- |
| RC 内部、TUI、录制等改动，策略看到的契约不变；或只改 example | `python3 scripts/test_scope.py` | 运行完整 example 基线；具体模块改动另补对应 RC 单测。链只启动两个 example，其他业务策略不参与。 |
| 只改某个策略的实现、manifest、镜像或依赖 | `python3 scripts/test_scope.py --policy llm_failure` | example 基线加该来源的策略套件。支持 hold、llm_direction、llm_failure、pi05、qwen35_single、qwen35_twostage、qwen35_history、qwen35_history_lift2、internvla_maas；可同时列多个来源。 |
| 同时修改多个策略的实现或依赖 | 例如 `python3 scripts/test_scope.py --policy qwen35_single qwen35_twostage` | 单选 Qwen 测该入口、部署和 worker；两个都选时再加配置、调度、后端和 CUDA 用例。每个策略独立持有业务实现；若同时修改两个 LLM 的 provider 读取逻辑，选 llm_direction 和 llm_failure。 |
| RC↔policy/SDK 公共契约改变 | `python3 scripts/test_scope.py --interface` | 整个 pytest 套件，含 RC、SDK、example、业务策略和跨策略对照；按所需环境追加 Docker/模型兼容性验证。 |

所有命令都可加 `--collect-only` 先看范围。无参数的 `python3 -m pytest` 仍表示全套。新增策略时，将该来源的专属测试登记在 [test_scope.py](../scripts/test_scope.py)。仅改 RC 实现并不自动升级范围；如果回调签名、action/state/images 形状、命令/manifest、热改确认、退出/租约证明或 MCP 等待 policy 结果的语义变了，即使函数名没变，也属于接口变更。策略专属模型输出、权重、CUDA 和供应商协议由对应策略验证，不能用 example 推断。

**example 基线的证据**来自 [策略回调](../tests/test_example_policy.py)、[真实 worker 和重复实例](../tests/test_example_worker.py)、[RC/TUI/MCP 闭环](../tests/test_example_controller.py)、[独立外部读者](../tests/test_example_reader.py) 和 [ROS 仿真](../tests/test_example_ros.py)。它覆盖 sync/async、qpos/epos/mixed、慢推理缓存、观测/图像、监控、录制/快照、姿态存取、配置、全局状态、暂停/完成/终止/故障与控制权。RC 故障注入、部分发布、超时清理等专项仍保留原有单测。

容器生命周期、IPC 或部署改动时，在默认基线之外运行 `./setup.sh --policies example` 和 `python3 tests/example_docker_smoke.py --evidence-dir /tmp/example-evidence`。该脚本不需要 hold 或其他策略。ROS 缺失时相应测试会跳过；在已 source Humble/ros_ws 的环境用同一入口即可执行。RC 镜像不再包含 ROS，可以使用 ACOne connector 的测试镜像跑 ROS 用例，无须在宿主安装 ROS：

```bash
docker build -f connector/acone/Dockerfile --target test -t rc-example-tests:local .
docker run --rm --network none --shm-size=512m --user "$(id -u):$(id -g)" \
  --workdir /workspace --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  -e HOME=/tmp -e PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 -e 'PYTEST_ADDOPTS=-p no:cacheprovider' \
  -e PYTHONPATH=/workspace/src:/workspace/packages/policy-sdk/src:/workspace/packages/connector-sdk/src \
  -e 'RC_SOURCE_MODULES={"robot_control":"/workspace/src","rc_policy_sdk":"/workspace/packages/policy-sdk/src"}' \
  rc-example-tests:local python3 scripts/test_scope.py
```

跨容器 ROS 验收使用 `python3 tests/example_docker_smoke.py --ros --evidence-dir /tmp/connector-ros`：RC 镜像必须无 rclpy，ACOne connector 与模拟器分别运行在独立容器中，共享 anchor IPC 并使用隔离的 namespace/domain。详情见 [connector 说明](connectors.md)。

### 0.13 同进程重初始化、实例与整链重载

`tests/example_docker_smoke.py` 的 `result.json.same_process` 给出两次原进程重初始化的前后对照，分别使用空补丁和冷字段补丁。目标的容器 ID、host、runtime、PID 和模块 ID 应一致，其 context ID 每次改变，目标配置和链版本各递增一次。另一实例的容器、进程、context 和版本均应一致。宿主接口套件另外用失效的临时 main.py 验证同进程路径只使用已加载模块。

验收脚本在这两次命令期间拦截 Docker 调用，任何调用都会令用例失败。随后模拟 SDK 已完成初始化、但 RC 收到错误确认。字段 `retry_after_reinitialize` 记录故障与手动恢复时的身份，核对故障阶段保留全部容器、配置维持在已提交版本，冷重试只替换指定实例。

2026-11-02 验收中，宿主完整接口套件 1224 项通过、18 项跳过，Linux / ROS 2 Humble 完整接口套件 1111 项通过、19 项跳过。真实 Docker 自链使用 Python 3.10.12 的 RC 和 Python 3.14.9 的 example，目标配置版本从 4 到 5 再到 6，容器、host、PID 9 和模块身份保持一致。ROS 仿真闭环和本地 Chat / Decisions HTTP 替身均已验证，真实供应商、GPU 模型和机器人硬件未参与本轮验收。

`tests/example_docker_smoke.py` 在同一条 `example → example` 链上对照两种 effect。`result.json` 的 `reload_identities` 记录每一步的容器 ID、host 身份、执行进程身份和配置版本。局部重载只应更换目标实例，整链重载应更换两者。目标创建回执丢失以及下一次 host 就绪失败时，脚本检查只剩原来的健康成员，并通过 `retry-reload example-1` 恢复。

检查 `cleanup_exit` 可核对最后的 RC 退出码。故障注入可能在新容器安装信号处理前将其停止，脚本要求强制退出记录准确对应这一次创建尝试，其余成员正常释放。退出后还会核对图像池、容器和共享卷。

## MCP 策略管理验收

`tests/test_mcp_management.py` 使用正式 example 和真实 host/worker 验证只读能力发现、HTTP/本地命令、严格 JSON 参数、重复实例寻址、链 apply/save 分离、三种 effect、失败实例冷重试、独立录制与等待任务、代次取消及 YAML 原子提交。另覆盖合法的同名 `record` 来源和 `return_and_wait` 策略命令，避免帮助或内部请求路由混淆。完整接口回归运行 `python3 scripts/test_scope.py --interface`。

`tests/example_docker_smoke.py` 增加真实 HTTP MCP 验收。`result.json.mcp_management` 记录链应用、同 PID 重初始化、目标重载、失败后冷重试和整组重建的容器/host/runtime/PID/版本；`recording` 与 `outcome` 核对 policy 运行时独立录制及原等待任务完成。脚本仍只使用 example，最终 `cleanup.json` 验证本次容器和卷为空；既有 lost-create 注入导致的强制退出码单独记录，不伪装成正常清理。

2026-11-03 最终验证：宿主 Python 3.11 完整 `--interface` 为 **1362 passed / 18 skipped / 13 个既有 Pillow 弃用告警**；Humble/Python 3.10 的新管理用例与真实 ROS 模拟器为 **17 passed**；真实 Docker 的 **7 组检查通过**，RC 使用 Python 3.10.12，example 使用 Python 3.14.9。源代码通过 Python 3.10 语法检查。未调用真实模型供应商、GPU 模型或机器人硬件，跳过不计通过。

证据：`/private/tmp/rc-mcp-management-interface-final.log`、`/private/tmp/rc-mcp-management-linux-final.log`、`/private/tmp/rc-mcp-management-docker-final/{result.json,cleanup.json}`。最终资源复核另存 `/private/tmp/rc-mcp-management-cleanup-final.json`。

## InternVLA MaaS 224×224 图像发送（2026-11-05）

按用户确认的方式，在 MaaS 后端发送前用双线性插值等比缩放、居中补黑边，三路输出固定为 RGB `uint8[224,224,3]` ndarray。原始观测、RC/SDK 契约、相机映射和动作逻辑不变。新增 `resize` 分项及 `last_source_image_bytes`，`last_image_bytes` 统计实际发送图像，固定为 451,584 字节。

- 缩放专项 **12 failed → 12 passed**，覆盖横竖图、奇数尺寸、极窄图、非连续数组、只读输入、RGB 通道和已为 224×224 时像素保持；真实本地 WebSocket 验证三路完整 224×224 数据和消息大小。已知时钟确认三路 resize 合计 3 ms 并计入 prepare。
- 受影响的 MaaS 图像、backend、timing、monitor、runtime、worker、部署发现七个文件 **71 passed**，4.65 秒。没有运行全量回归、ROS、Docker 或真实 MaaS/硬件。
- 新增策略依赖 Pillow 12.3.0 及哈希锁，其余依赖版本不变。Python 3.11 环境通过离线、哈希校验的安装 dry-run（5 个包，无需变更）。本次未重建镜像；部署需执行 `./setup.sh --policies internvla_maas` 后冷重载策略。

证据：`/private/tmp/rc-maas-resize-red.log`、`/private/tmp/rc-maas-resize-green.log`、`/private/tmp/rc-maas-resize-targeted.log`、`/private/tmp/rc-maas-resize-lock.log`。

## InternVLA MaaS 策略内分阶段计时（2026-11-05）

在原有请求总延迟和失败次数之外，增加 14 个阶段的平均耗时、最近耗时和样本数，以及图像/请求/响应字节数。覆盖快照提交、网络线程派发、准备、打包、发送、响应等待、解码、校验、服务端自报时间、后台推理、结果就绪等待、结果收集、夹爪后处理和完整回调。复用 SDK 原调度实现及完成时间戳，不修改 RC/SDK 公共接口。

- 新增专项先 **10 failed**，实现后 **10 passed**。已知时钟结合真实本地 WebSocket 核对打包 2 ms、发送 4 ms、等待 50 ms、解码 3 ms，以及实际消息字节数；服务端时间缺失/非法不伪造样本、不拒绝合法动作。
- 受影响的 timing、monitor、backend、runtime、worker 五个 MaaS 文件最终 **58 passed**，4.46 秒。验证就绪后等待 125 ms 的采纳延迟、缓存回调不新增模型请求计数、失败阶段计时、原失败次数与主动取消口径、只读 monitor 和普通 reset 保留统计。
- 新阶段只保存平均值、最近值和计数，不增长历史数组；合并快照满足 SDK 的内建标量、项数和大小限制。分项存在包含关系及不同样本群体，不能直接相加。没有启动全量回归、ROS、Docker、真实 MaaS 或硬件，RC 端计时不在本次范围。

证据：`/private/tmp/rc-maas-detailed-red.log`、`/private/tmp/rc-maas-detailed-green.log`、`/private/tmp/rc-maas-detailed-targeted.log`。

## InternVLA MaaS async 配置与请求监控（2026-11-05）

取消策略专属的 sync/每步回调限制及禁止所有热修改的检查，模板默认 async，支持模式和调用间隔的白名单热修改。新增只读 monitor：平均、中位、上次请求延迟（毫秒）与失败次数；统计当前 context 内实际发送的已结束请求，排除握手和主动取消。

- 按用户要求仅验证受影响的 MaaS 配置、部署发现、monitor、backend、runtime、worker 六个文件：**82 passed**，4.63 秒。没有运行全量回归、example 套件、ROS 或 Docker。
- 精确时钟用例核对奇偶样本中位数、平均值、失败次数去重，以及成功、服务端拒绝、非法动作、断线、超时、主动取消的统计。请求在途时反复读 monitor 不会发请求或等待推理。
- 真实本地 host/worker 验证 monitor 独立响应、共享图像释放、版本确认、sync/async 和调用间隔热修改；运行中的 RC 保留 worker 和已有连接，普通 reset 保留统计。冷重载和故障恢复沿用已有行为。

证据：`/private/tmp/rc-maas-monitor-red.log`、`/private/tmp/rc-maas-monitor-worker-red.log`、`/private/tmp/rc-maas-monitor-targeted-final.log`。中间新增断言忽略了 resume 原有的重连，改为比较热修改前后的会话数量后通过；没有为此修改生产生命周期逻辑。未连接真实 MaaS 服务或硬件。

## InternVLA MaaS ndarray 编码（2026-11-05）

收发切换为用户已验证的 `diagnose_maas_ndarray.py` 编码：字节键 `__ndarray__` / `__npgeneric__`，独立数组副本，4 MiB 入站限制。移除 `msgpack-numpy`，其余锁定依赖版本不变；RC↔SDK 契约未变。

- 编码专项 **64 passed**；独立测试参考的四个函数与成功脚本 AST 一致。新增测试先在旧实现出现 **56 failed、8 passed**，替换后通过。
- 无 `msgpack-numpy` 的 Python 3.11 环境，所选 `--policy internvla_maas` 范围 **207 passed、1 skipped**，71.74 秒；宿主缺 ROS 的跳过不计通过。新 Linux/Python 3.11 测试镜像中策略套件 **124 passed**，0.83 秒。
- 已启动的 MaaS 隔离 Docker 验收 **4 组通过**，RC 退出码 0；example 自链 **7 组通过**，既有故障注入的强制清理如实保留 RC 退出码 1。两套临时部署清理完成。
- ROS 补跑 **1 failed**：只读测试容器未设置可写 HOME，ROS 无法创建 `/root/.ros/log`，未进入策略执行。用户随后要求停止扩展回归并直接提交推送，因此不再补跑。未运行全仓 `--interface`，未连接真实 MaaS 或机器人。

证据：`/private/tmp/rc-maas-ndarray-{codec-red,codec-green,scope,linux,ros}.log`，以及 `/private/tmp/rc-maas-ndarray-docker/`、`/private/tmp/rc-maas-ndarray-example-docker/` 下的结果与清理记录。正式 MaaS 运行镜像和专用测试镜像构建成功。

## InternVLA MaaS 网络策略（2026-11-04）

实现与配置见 [网络策略说明](../policy/internvla_maas/README.md)。本轮没有修改 RC↔SDK 公共契约，也没有修改 Qwen 实现；服务地址、身份和凭据由用户提供。

- 最终 `python3 scripts/test_scope.py --policy internvla_maas`：**127 passed、1 skipped**，33.01 秒。覆盖 example 基线及配置、msgpack-numpy 安全解码、真实 WebSocket、异步补块、夹爪开关、取消/故障、部署发现与真实 host/worker 冷切换。宿主缺少 ROS 的跳过不计通过。
- Python 3.11.17 / Linux 策略测试镜像：**71 passed**，0.70 秒；单独 Humble / Python 3.10 example ROS 仿真：**1 passed**，2.80 秒。
- 新策略隔离 Docker：**4 组检查通过**，RC Python 3.10 → policy Python 3.11，实际动态容器、共享图像、MaaS 二进制请求、冷切换、stop 故障与手动恢复，RC 退出码 0。临时容器、卷和网络已清空。
- example / example 自链隔离 Docker：**7 组检查通过**，包含实例隔离、重初始化/重载/重试、HTTP MCP 和录制；既有 lost-create 故障注入产生的强制清理被正确记录为 RC 退出码 1，验收脚本退出 0，清理检查通过。
- 依赖、构建工具和测试依赖均有固定版本及哈希锁。标准镜像 `robot-control-compose-internvla-maas:local` 构建成功；源码语法与 `git diff --check` 通过。自审补齐空 userinfo 地址拒绝、float32 溢出和缺失可选依赖时的测试收集。

证据：`/private/tmp/rc-internvla-maas-scope-final.log`、`/private/tmp/rc-internvla-maas-linux-final.log`、`/private/tmp/rc-internvla-maas-ros.log`，以及 `/private/tmp/rc-internvla-maas-docker/` 和 `/private/tmp/rc-internvla-maas-example-docker/` 内的结果与清理 JSON。本轮为单智能体实施与自审；Docker 环境是本机 OrbStack Linux/arm64。真实 MaaS 服务、目标部署平台和机器人硬件尚未验收，未调用附件中的地址或凭据。

## 按运行实例名 set 配置（2026-11-04）

`set <实例ID>.<字段路径> <JSON值>` 支持单策略与原生链。唯一来源直接用策略名，重复来源使用 `-0/-1/...`。链内按热字段白名单提交，只更新目标实例及逻辑链版本；其他实例的配置、context 与执行进程不变。受理时捕获运行组、版本与动作代次，避免排队编辑在暂停、接管或切换后落到新实例。单策略的 `set policy.*` 及冷配置路径保持兼容，链内冷参数沿用显式实例重载命令。

按本次用户要求仅执行受影响范围，策略验证使用 example：

- `python3 scripts/test_scope.py`：**76 passed、1 skipped**，53.25 秒。包括具名唯一/重复实例、精确更新、原配置引用与 context、JSON 字符串/null、保存、歧义与冷字段拒绝、控制代次、MCP 和迟到 ACK；缺少 ROS 的跳过不计通过。
- RC 配置、链解析、控制器、命令解析、实例保存及客户端代次单测：**66 passed**，10.57 秒。该范围使用 example 或测试自带的轻量替身，不启动业务模型策略。
- Python 3.10 语法检查 5 个文件及 `git diff --check` 通过。未运行全量接口、Qwen/其他业务策略套件，也未启动 Docker、模型或硬件。

证据：`/private/tmp/rc-named-set-red.log`、`/private/tmp/rc-named-set-green.log`、`/private/tmp/rc-named-set-example-scope.log`、`/private/tmp/rc-named-set-rc-scope.log`。

## 注册热配置命令与 Qwen switch-task（2026-11-04）

两个 Qwen 注册 `switch-task <str>`，handler 返回 `params.task` 热补丁，RC 通过新增的协商命令 effect `update_config` 复用现有 SDK 热更新事务。example 的 `set-message` 展示相同公共契约。链内只修改明确目标实例，保留所有 context、进程和容器，目标及逻辑链版本递增；MCP 接管要求和控制代次复核保持有效。

- 最终 `python3 scripts/test_scope.py --interface`：**1479 passed、27 skipped**，531.66 秒，13 条既有 Pillow 弃用告警。跳过项不计通过。覆盖注册、能力协商、热白名单、原配置引用、链实例隔离、保存、MCP 权限及暂停后迟到结果/ACK 不提交。
- 自审发现原 `PolicyProcess` 热更新 ACK 未核验 `config_version`，新增缺失值、旧版本和字符串版本故障注入，先 3 项失败后修复；相关命令、Qwen worker、热更新与冷重载专项 **54 passed**。
- Humble / Python 3.10 新命令、ACK 校验及 example ROS 仿真：**22 passed**，12.60 秒。
- 真实 Docker 的两个 Qwen 通过 `switch-task` 更新任务并恢复默认任务，容器、host、runtime 和 PID 保持一致，测试后端 load/warmup 各一次，配置版本 1→3，RC 退出码 0。
- example / example 自链的 **7 组 Docker 检查通过**，实际 RC Python 3.10 → example Python 3.14。注册热命令更新第二个实例时，第一实例版本维持 4，目标从 4 到 5，两者容器、host、PID、模块和 context 不变；命令期间禁止调用 Docker。既有 lost-create 故障注入正确保留 RC 强制清理退出码 1，验收脚本退出 0。

证据：`/private/tmp/rc-switch-task-interface-final.log`、`/private/tmp/rc-switch-task-ack-green.log`、`/private/tmp/rc-switch-task-humble.log`，以及 `/private/tmp/rc-switch-task-docker/`、`/private/tmp/rc-switch-task-example-docker/` 的结果与清理 JSON。本轮单智能体实施和自审；测试部署及专用镜像标签已清理，保留其他任务使用的 Docker 引擎。未调用真实模型、供应商或机器人硬件。

## Qwen 任务热更新第一阶段（2026-11-04）

本节记录首次通过 `set policy.params.task` 接入热参数时的验证：两个 Qwen 复用既有白名单热补丁与配置版本确认，当时未改 RC/SDK 公共协议；后续注册命令的契约扩展见上一节。使用与旧配置迁移见 [Qwen 策略说明](../policy/qwen35_single/README.md#任务文本热更新)。

- `python3 scripts/test_scope.py --policy qwen35_single qwen35_twostage`：**109 passed、9 skipped**，36.12 秒。包含 example 基线、两个正式入口和真实 RC/host/worker、热补丁版本同步、保存与 null 回退；宿主缺少模型依赖和 ROS 的跳过不计通过。
- Linux / Python 3.11.17 的 Qwen 配置、入口、调度、CPU 后端和 CUDA 回退套件：**67 passed、4 skipped**，11.99 秒。实际两阶段缓存测试确认任务变化后立即重新生成子任务、模型对象保持不变；4 项跳过均需要 NVIDIA CUDA 设备。
- 独立 Humble / Python 3.10 example ROS 仿真：**1 passed**，3.14 秒。
- 隔离 Docker 中分别运行两个 Qwen，RC Python 3.10 → policy Python 3.11。任务覆盖及 null 回退前后，容器 ID、host、runtime 和执行 PID 均保持一致，配置版本从 1 到 3，测试后端 load/warmup 各一次；更新期间拦截 Docker 调用，确认热更新路径不调用 Docker。RC 退出码 0，临时容器、卷和网络已清空。

CPU 容器首次运行遗漏正式 Compose 所需的 Transformers 补丁挂载，导致 `test_load_warmup_uses_checkpoint_config_and_rejects_wrong_chunk` 和 3 个 `test_fallback_temporaries_stay_on_the_input_device` 参数用例失败；补齐只读挂载后的上述最终套件全部通过，无需修改厂商源码。

证据：`/private/tmp/rc-qwen-task-scope.log`、`/private/tmp/rc-qwen-task-model-tests-final.log`、`/private/tmp/rc-qwen-task-ros.log`、`/private/tmp/rc-qwen-task-docker/{result.json,cleanup.json}`。本轮单智能体实施与自审；Docker 使用本机 OrbStack，模型推理由测试后端代替，未执行真实 GPU 模型或机器人硬件验收。

## 历史验收

本文保留 2026-10-27 的历史验收记录；源码外置后的复现命令已更新，最新验收见下方“源码挂载验收”。

日期：2026-10-27。代码与部署结构已重构为基础 RC/anchor 两服务、按目录发现的独立策略容器。本页区分自动化测试、真实容器、模型和硬件证据。

## 已执行的验证

| 验证 | 结果 | 范围 |
| --- | --- | --- |
| 完整主机套件 | **697 passed，27 skipped** | macOS/arm64，Python 3.11 测试环境；控制循环、配置、命令、MCP、SDK 进程、外部资源、TUI |
| 完整 Linux 套件 | **672 passed，27 skipped** | ROS 2 Humble / Python 3.10.12 容器，包含实际 ROS 仿真闭环；只读挂载当前源码 |
| 实际动态 Docker 验收 | **10 组通过** | 基础拓扑、IPC、Py3.10→3.14、冷切换、故障与恢复，见下表 |
| 公共宿主 down | **通过，返回 1** | 已确认强制清理的 RC 非零退出码被保留，策略与基础资源完成收尾 |
| 镜像构建 | **RC、hold、anchor、测试镜像成功** | SDK/RC 0.9；RC 内 Docker CLI 28.5.1、Compose 2.39.1 |
| 正式 RC 镜像源码审计 | **12 个关键模块逐字节一致** | controller、runtime manager/spec、external、host client/process/chain，以及 SDK catalog/host/lifecycle/supervisor/commands |
| 本机配置值审计 | **8 份保留** | 除 schema、移除 host/deployment 表和 Qwen 相对模型路径的预定转换外，既有值不变；备份保留在 .setup/backups |
| 构建声明与插件发现 | **通过** | 无 example 的未知策略、发现不 import、两 Qwen 独立完整声明与相同有效输入去重、Dockerfile 专属 ignore 差异检查 |

两套完整测试各有 13 条既有 Pillow/Textual-image 弃用警告。跳过项依赖特定平台、可选模型依赖或外部服务；没有将跳过项记作通过。

真实容器环境为本机 OrbStack 的 Linux/arm64 daemon：Engine 29.8.2，宿主 Compose 5.5.0。它的 socket GID 在容器内会映射，因此验收夹具探测了实际映射 GID。正式部署目标仍为本地 Linux Docker；这份证据不代替 Linux/amd64 目标机、rootless、远端 daemon 或其他 Desktop 平台验收。

## 实际容器检查

`tests/dynamic_docker_smoke.py` 为每轮创建唯一临时部署，使用含空格的项目路径及项目外 bind mount。容器内脚本 `tests/container_dynamic_smoke.py` 使用真实 RC、SDK host、Docker CLI 与 mock 机器人。

1. startup_policy=null 时只有两个基础容器，active_policy 为空；静态帮助不会创建策略。
2. RC Python 3.10 调用 Python 3.14 策略；策略实际映射只读图像，独立外部目录挂载可读。
3. 同 ID 冷切换产生新 CID 与 host instance；默认 switch 不复位。
4. `[a,b,a]` 只创建两份实例；切 `[b,c]` 的事件记录证明全部旧 CID 删除后才执行新 create；同镜像仍有独立实例，歧义裸命令被拒绝。
5. 删除旧 Compose 文件后仍可按 CID/标签清理。
6. 单策略 initialize 失败后，活动身份清空且无残留容器。
7. 链部分初始化失败时回收本次全部成员。
8. release 卡死时在预算内强制清理，后继保持暂停，强制/非零事实保留；显式 --reset 由控制线程执行运动。
9. 新 RC run 回收旧 run 的持久创建意图/容器，并在同 anchor 中实际清除旧 POSIX 图像池段。
10. anchor 更换/出现多实例时清理旧策略并阻止新组启动。另有故障注入覆盖同 CID 重启造成的 IPC 代次变化。

公共 `down` 额外验证：读取已停止 RC 的 ExitCode，在精确资源证明齐备后移除基础设施，仍返回 1；没有把 Docker stop 命令成功误认为 RC 正常退出。

独立代码审查发现的退出期限、退出码、共享内存恢复、有效 build ignore、业务 JSON 误识别问题均已补失败回归后修复。额外回归覆盖启动恢复失败时禁止手动 switch、daemon 身份变化、无法确认的迟到 create、错误标签、外部 instance 变化和目录别名。

## 复现

主机测试需要合适的开发依赖及本地 UDS/POSIX 共享内存权限：

```bash
python3 -m pytest -q
```

Linux 验证镜像：

```bash
docker build -f docker/rc.Dockerfile --target test -t robot-control-compose-dynamic-tests:local .
docker run --rm --shm-size=512m \
  --user "$(id -u):$(id -g)" --workdir /workspace \
  --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  -e HOME=/tmp \
  -e RC_SOURCE_MODULES='{"robot_control":"/workspace/src","rc_policy_sdk":"/workspace/packages/policy-sdk/src"}' \
  -e PYTHONPATH=/workspace/src:/workspace/packages/policy-sdk/src:/workspace/packages/connector-sdk/src:/workspace/ros_ws/src/robot_control_test_sim \
  -e PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  robot-control-compose-dynamic-tests:local \
  python3 -m pytest -p pytest_asyncio.plugin -q -p no:cacheprovider
```

动态容器验收需先构建上述测试镜像、hold 与 anchor：

```bash
docker build -f policy/hold/Dockerfile -t robot-control-compose-policy:local .
docker build -f docker/ipc-anchor.Dockerfile -t robot-control-compose-anchor:local .
python3 tests/dynamic_docker_smoke.py
```

脚本仅操作带本轮唯一部署/project 标签的资源，结束后清理其容器、网络和卷。生产启动与收尾使用 `scripts/compose.sh`，不复用验收部署身份。

本轮完整日志、事件时间线和源码哈希保存在执行机 `/private/tmp/rc-dynamic-runtime-20261027/`。它们是运行证据，不提交仓库。

## 未执行的验收

未连接实体机器人、未启动厂商硬件驱动、未调用真实付费供应商、未进行真实 Qwen/GPU 推理。本轮没有构建或验证新的 Qwen CUDA 镜像运行实例；Qwen 本机子进程测试使用轻量 backend。实际权重、CUDA/NVIDIA runtime、模型延迟、真实相机和动作保持/显式复位仍需在目标机器按现场安全流程验收。

## 源码挂载验收

`tests/source_mount_docker_smoke.py` 创建独立临时部署，检查新依赖镜像在无源码挂载时无法导入项目包，然后修改临时实现包、冷切换 policy、完整退出并重建 RC/anchor。它核对源码行为变化而镜像 ID 不变，包含原子替换编辑、故意保留的旧宿主字节码、只读挂载、缺失源码拒绝和失败后的清理。

可通过 `RC_TEST_RC_IMAGE`、`RC_TEST_ANCHOR_IMAGE`、`RC_TEST_WORKER314_IMAGE` 指定本次新建的依赖镜像；全局字典夹具另接收 `RC_TEST_WORKER311_IMAGE`。这些变量只选择验收镜像，不修改本机部署配置。测试 RC 镜像应选 `docker/rc.Dockerfile --target test`。默认动态镜像名称与上方构建命令一致。

```bash
docker build -f policy/llm_failure/Dockerfile --target runtime -t robot-control-compose-llm-failure:local .
export RC_TEST_RC_IMAGE=robot-control-compose-dynamic-tests:local
export RC_TEST_ANCHOR_IMAGE=robot-control-compose-anchor:local
export RC_TEST_WORKER314_IMAGE=robot-control-compose-policy:local
export RC_TEST_WORKER311_IMAGE=robot-control-compose-llm-failure:local
python3 tests/source_mount_docker_smoke.py
python3 tests/global_state_docker_smoke.py
python3 tests/dynamic_docker_smoke.py
```

Qwen 导入探针 `tests/qwen_image_probe.py` 必须使用与策略 Compose 相同的 SDK、实现包、vendor 和直接补丁挂载；默认检查 qwen35_single，使用 `--policy qwen35_twostage` 检查双阶段策略自己的目录。它核对第三方版本、源码位置和当前源文件哈希，不要求本项目包的已安装发行元数据。Pi05 的 PYTHONPATH 不包含 Qwen vendor，使用镜像中锁定的官方 LeRobot。GPU 和真机测试仍需在目标设备另行执行。

## 链按环节独立实例验收（2026-10-30）

`[a,b,a]` 现展开为 `a-0,b,a-1`，每个环节独占容器、host、执行进程和 context；本节替代历史验收中的“重复成员复用”行为描述。

- 完整主机套件：1003 passed，29 skipped；Python 3.11。另有 globals 超时断言跨版本兼容检查 5 passed。
- 完整 Linux/ROS 套件：940 passed，31 skipped；ROS 2 Humble / Python 3.10。两套完整测试各有 13 条已有 Pillow 弃用警告。
- 动态 Docker：10 组通过，覆盖三个独立容器/host/socket/网络、原源码路径与同镜像、独立 command/monitor、整组退出屏障、部分启动失败、恢复及强制清理。强制清理场景的公共 down 按预期返回 1，验收脚本返回 0。
- 完成结果 Docker：4 组通过，覆盖实例来源、MCP 等待/取消和后续环节故障。
- globals Docker：4 组通过，实际运行 Python 3.10 RC、Python 3.14 的两个独立 a 实例及 Python 3.11 b；共享状态、独立计数、重启及 SIGTERM 持久化正常。
- 独立审查发现的重新应用链读取旧来源缓存问题已补失败回归并修复。真实 Docker 暴露的 Compose 默认网络重名问题已修复，自有网络名称随实例项目隔离。

本轮证据目录为 `/private/tmp/rc-chain-instances-20261030/`：`host-final.log`、`linux-verified.log`、`dynamic-final/container-smoke.json`、`result/cross-container.json`、`global-final/cross-container.json`。验收仅使用隔离测试部署，结束后清理所属容器、网络和卷；未进行真实机器人、GPU 模型或外部供应商验收。

## 连续录制验收（2026-10-30）

`record` 增加本地异步启停、独立 30 Hz 三路 PNG 与四份 CSV、完整状态／控制／逐实例推理日志、配置历史及 LLM 请求诊断。格式、容量和复现命令见 [连续录制文档](recording.md)。

- 最终完整主机套件：**1107 passed，29 skipped**，Python 3.11，366.58 秒。
- 最终完整 Linux/ROS 套件：**1032 passed，31 skipped**，ROS 2 Humble / Python 3.10，494.54 秒。两套各有 13 条既有 Pillow 弃用警告。
- 录制专项覆盖真实 SIGSTOP、真实 EFBIG、字节／条数满、SDK 日志洪泛、反复启停、跨会话缺口归属、绝对退出期限和 SIGTERM。采样额度保留到 writer ACK；强制退出后核对进程、管道和线程清理。无发送、混合模式、逐臂部分失败、MCP 控制权和推理等待均有回归。
- 独立审查发现并修复两项问题：图像池所有槽都被租约占用时，记录实际推理 lease 对应帧；普通日志条数达到上限时，仍给关键回执和 LLM 事件保留额度。另补齐最终退出摘要和诊断缺口的会话归属。

最终性能验收在其他 CPU 测试暂停后顺序运行，源码哈希与最终生产代码一致：

| 指标 | 动态 Mock，三路 640×480 | 真实 ROS，三路 64×48 |
| --- | ---: | ---: |
| 时长／采样数 | 60.002473 秒／1801 | 10.007046 秒／301 |
| 每路有效写入频率 | 30.015430 Hz | 30.078807 Hz |
| 采样截止缺口／缺失帧／附加丢弃 | 0／0／0 | 0／0／0 |
| 在途信用峰值／观察到的采样队列峰值 | 149797208 bytes／8 | 2590160 bytes／7 |
| 控制发送调用耗时 P99／最大 | 0.591678／3.159562 ms | 2.724691／15.702186 ms |
| 含尝试记录的控制调用耗时 P99／最大 | 1.605949／5.437733 ms | 3.900286／15.935230 ms |
| 实际逐臂发布／HTTP 和 SDK 请求对 | 5998／59 | 1000／10 |

Mock 核对 5394 个唯一采样 PNG 的无损像素、四份 CSV 和实际 policy 输入图像。每路重复帧和来源序号跳跃均为 3，均保留在采样元数据中。ROS 的 1000 次 ACOne 成功发布与 simulator 收到的模式和数值完全一致；本节调用耗时不代表真实机器人响应延迟。两轮停止后待写入归零，无强制清理或残留容器。

证据位于 `/private/tmp/rc-record-20261030/`：`host-verified.log`、`linux-verified.log`、`manual-mock60-isolated/report.json`、`manual-ros10-isolated/report.json` 及各自的 `teardown.json`。另保留主机套件并发时出现 1 个采样截止缺口的失败压力报告 `manual-mock60-frozen/report.json`，没有改低门槛记为成功。

首次全套暴露的旧时间夹具、监控后台发送时序及新增日志缺口断言已更新。一次 Linux TUI 退出非零未在隔离 4 项测试及最终全套中复现；测试现在保留末尾 RC 事件用于诊断，最终运行日志含 `rc.exit`、`writer_drained=true`、`exit_code=0`，没有为此修改退出控制逻辑。

验证没有增加 SDK 依赖、Compose 服务、厂商驱动或模型请求重试。此轮 Mock、ROS 仿真和本地 HTTP 验收不替代真实相机、机器人或外部模型供应商验收。

## llm_failure GPT Decisions 后端（2026-10-30）

新增默认关闭的 `params.decision_api` 配置组，启用后使用独立模型、地址、密钥和超时；关闭或省略时保持 DeepSeek。请求为单个二选一 choice 问题，严格解析为失败布尔值，不重试或回退。详见 [配置与协议说明](../policy/llm_failure/README.md)。

- 锁定 OpenAI SDK 3.37.0 的完整主机套件：1131 passed，29 skipped，363.78 秒。
- 完整 Linux/ROS 套件：1032 passed，31 skipped，412.74 秒。两套均有 13 条已有 Pillow 弃用警告。
- Python 3.11 / OpenAI 3.37.0 策略镜像：95 passed，24.86 秒；只读补入已锁定的纯 Python YAML 依赖，不改变模型镜像依赖。RC/worker 集成在主机套件中验证。
- 覆盖实际本地 HTTP 请求、非法选项、拒答、超时、选中配置的期限、两组密钥脱敏、token 用量、录制日志、reset 后迟到结果和请求期间图像租约释放。独立代码审查未发现待修问题。

首次 Linux 全套触发既有 MCP 测试的启动竞态：HTTP 服务已监听，但 policy 仍在 switch，提前 take 正常返回 `busy: switch`。通过延迟测试 worker 启动 0.2 秒稳定复现，仅修正测试为等待初始化完成；生产控制逻辑不变。相同延迟实验转绿，MCP 模块 7 项通过，随后完整 Linux 套件通过。

证据目录：`/private/tmp/rc-failure-decisions-20261030/`，包括 `host-full.log`、`linux-verified.log`、`policy-linux-verified.log`、`incremental-sdk337.txt` 和 `mcp-delayed-start-red.txt` / `mcp-delayed-start-green.txt`。未调用真实收费 API，也未进行真机准确率验收。

## WebUI 直接命令与文本批次（2026-11-04）

新增 `llm=false` 直接模式和人工 MCP `tui_command/cancel_tui_command`。文本批次无需 take、FIFO 逐行执行；本地 TUI 的非空输入清除未开始的行和等待批次，当前已执行行沿用原生命周期。实现及使用见 [WebUI](webui.md#直接命令模式) 和 [MCP](mcp.md#人工文本批次)。

按用户指定范围仅做定向验证，未运行 `test_scope.py`、`--interface`、整个 tests 目录或业务 policy 套件：

- 新增批次、MCP、两个真实 example worker 场景，以及直接受影响的 TUI/控制代次/日志节点：**43 passed**，6.75 秒，`/private/tmp/rc-tui-targeted-backend-final.log`。
- 六个相关前端测试文件：**43 passed**，`/private/tmp/rc-tui-web-unit-final.log`；类型检查和生产构建通过，`/private/tmp/rc-tui-web-build-final.log`。
- 固定 Nginx/njs 的真实容器配置测试：**10 passed**，2.77 秒，`/private/tmp/rc-tui-nginx-green.log`。初次发现 njs 不支持对象展开，改用先验证字段再 Object.assign；依赖未新增。
- Chromium 开发模式：四个直接模式加一个 LLM 兼容用例 **5 passed**，14.4 秒；新增提交前停止竞态 **1 passed**，4.4 秒。日志为 `/private/tmp/rc-tui-dev-browser.log` 和 `/private/tmp/rc-tui-dev-stop-before-submit.log`。
- 生产 Nginx：LLM 同源代理兼容 **1 passed**，3.0 秒；读取真实 `{"llm":false}` 私有配置的五个直接模式用例 **5 passed**，11.8 秒。证明无需 API 配置、AI 路由返回 503、文本原文仅提交一次、无需 take、递归整批拒绝、停止保留当前行收尾、刷新不取消或重发。日志为 `/private/tmp/rc-tui-production-llm.log` 和 `/private/tmp/rc-tui-production-direct-verified.log`。
- 同一隔离生产部署运行 mock RC 和动态 example 容器，8 组 Compose/HTTP 检查通过；状态、直接配置、统一清理证据在 `/private/tmp/rc-tui-production/`。截图在 `/private/tmp/rc-tui-production-direct-verified/`，已核对三路实时 mock 图像、命令模式和批次成功回执。

额外修复经失败用例验证的边界：生命周期丢弃子命令后清除来源记录，已终态队列请求不再执行；TUI 清队列不撤销已经开始的 exit；敏感的非法本地输入仍清队列且不泄露；停止请求立即发出，不等待下一次回执轮询。

环境记录：旧 RC 镜像缺少已锁定的 ruamel.yaml；直接构建请求 Docker Hub 元数据时返回 401，改为继承本地 RC 镜像并安装仓库完整 rc.lock，通过 pip check。随机宿主端口在测试容器重启后变化，重新发现端口后生产用例通过。失败启动和旧端口用例不计通过。没有真实模型、ROS 仿真或硬件验收，没有修改用户私有配置。

## WebUI 同源 AI 代理（2026-11-04）

真实 endpoint 与 Key 由 WebUI 服务端私有 JSON 提供，Nginx 代理固定 `/ai/chat/completions`；浏览器只读取 `enabled/model/timeout_s`。复用基础镜像自带 njs 生成启动配置，未增加生产 Node/Python 后端。迁移步骤见 [WebUI 配置](webui.md#api-配置)。

- 前端、共享配置与真实 HTTP 开发代理：**35 passed**，类型检查和生产构建通过。覆盖无 CORS、服务端鉴权、固定目标、超时、重定向和错误内容脱敏。
- 实际只读 Nginx/njs 镜像配置测试：**9 passed**，2.28 秒；检查非 root、0600 私有配置、公共 JSON、字面 URL、非法 JSON/配置不输出凭据。
- Chromium 开发模式：**9 passed**，23.1 秒；最终生产 Nginx 模式：**9 passed**，29.9 秒。实测上游 OPTIONS 返回 403 且没有 CORS 许可，页面仍能经同源代理完成查询/录制/复位/策略切换。浏览器请求无 AI Authorization，公共配置无 endpoint/key；MCP、三路预览、移动布局和错误时不执行操作均通过。
- 生产 Compose 另验证私有文件不在网页目录、独立 AI/MCP 鉴权、POST/JSON/Origin 约束、客户端 query 不能改写上游、字面 `$host` 不被展开、401 与 418 保留状态并隐藏错误内容。上游 URL 的字面 `$` 按 `%24` 发送；Nginx 内部 client-abort 499 不能配置为 error_page，配置生成显式排除它。
- 项目默认 example 范围：**60 passed、1 skipped**，46.27 秒；宿主缺少 ROS 的跳过不计通过。本轮没有修改 RC/SDK 契约或调用真实模型、机器人硬件；并行 Qwen 工作的验证另见其记录。

日志：`/private/tmp/rc-webui-ai-proxy-unit-final2.log`、`/private/tmp/rc-webui-ai-proxy-build-final2.log`、`/private/tmp/rc-webui-ai-proxy-nginx-final.log`、`/private/tmp/rc-webui-ai-proxy-dev-e2e-final.log`、`/private/tmp/rc-webui-ai-proxy-e2e-verified.log`、`/private/tmp/rc-webui-ai-proxy-example-scope.log`。隔离 Docker 与清理证据在 `/private/tmp/rc-webui-ai-proxy-docker-verified/`，标准镜像复核在 `/private/tmp/rc-webui-ai-proxy-delivery/`；浏览器截图单独保存在临时目录。实测环境为 macOS/OrbStack Linux arm64、Chromium，Browser 插件不可用，使用已有 Playwright。

以下首版记录保留当时浏览器直连 API 的历史实现和性能证据；当前部署使用上述同源代理。

## Vue 三路 WebUI 首版（2026-11-04）

采用 Vue 静态前端、独立 Nginx 容器和现有 RC HTTP/MCP。三路监控独立读取相机缓存，不使用 policy 共享图像租约。运行与配置说明见 [WebUI](webui.md)。

- 最终 RC 相关回归：58 passed，41.63 秒，覆盖预览、相机缓存、MCP、控制代次、CLI 和统一清理期限。
- 项目默认 `scripts/test_scope.py`：51 passed、1 skipped，30.54 秒；宿主缺少 ROS 的跳过不计通过。另在 Humble 容器运行 example ROS 仿真：1 passed，2.71 秒。
- Humble / Python 3.10 的最终预览与控制代次专项：10 passed，4.88 秒。
- 前端单测：12 passed；类型检查、Vite 生产构建通过。最终 Nginx 镜像的 Chromium 浏览器流程：7 passed，23.3 秒，覆盖路由刷新、窄屏底部输入框、复位、整份翻译校验、CORS/超时、录制例外、人工干预、策略自链切换与继续、过期状态及关闭后的订阅释放。
- 隔离 Docker 使用生产 Compose overlay、真实动态 example/self-chain 容器和本地兼容 API 替身。检查只读运行、无 Node 运行时、无共享内存/RPC/Docker socket 挂载、同源代理和 token 转发；非本地 Host 被代理拒绝。

本机 macOS / OrbStack Linux 容器，Chromium 156.0.8078.4 headless 活动页面，1440×900；三路带像素序号的 mock 源均为 640×480、30 Hz。example 自链保持 running，连续测量 600.009 秒。浏览器按 JPEG 中的源序号统计真正更新的图像，不把 RAF 或重复旧帧计为新帧。

| 相机 | 源帧率 | 编码帧率 | 输出帧率 | 浏览器实际新帧率 | 延迟 P50 / P95 / 最大值 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 左腕 | 30.00 | 29.93 | 29.92 | 29.89 fps | 50.8 / 58.8 / 473.8 ms |
| 头部 | 30.00 | 29.93 | 29.92 | 29.87 fps | 51.8 / 73.8 / 318.5 ms |
| 右腕 | 30.00 | 29.92 | 29.91 | 29.88 fps | 52.2 / 57.2 / 510.8 ms |

三路 10 分钟均值均超过 29.5 fps 门槛，浏览器脚本错误为 0。存在短时抖动：最低 10 秒窗口约 28.65 fps；末尾完整窗口恢复约 30.05 fps，未出现旧帧持续积压。延迟由 mock 源帧时间与浏览器取样时间计算，经一次跨进程时钟偏移校正，包含显示采样误差。

59 次资源采样中，RC Docker CPU 均值 30.26%，Nginx 1.52%（Docker stats 口径）；RC 内存范围 259.1–272.1 MiB、首尾 263.3→267.7 MiB，Nginx 8.47–9.64 MiB、首尾 8.93→9.07 MiB。浏览器渲染进程 CPU 均值约 35.7%，JS heap 4.15–5.69 MiB，后者包含测量脚本保存的延迟样本，不代表完整浏览器 RSS。资源有正常波动，未观察到随帧数持续积累的发送队列。慢读者、无订阅时停止编码、迟到编码结果和期限耗尽另有专项回归。

证据：`/private/tmp/rc-webui-benchmark-600/result.json`、`/private/tmp/rc-webui-docker-verified/`；最终页面截图在 `/private/tmp/rc-webui-delivery/`。最终日志为 `/private/tmp/rc-webui-rc-final.log`、`/private/tmp/rc-webui-scope-final.log`、`/private/tmp/rc-webui-humble-final.log`、`/private/tmp/rc-webui-example-ros.log`、`/private/tmp/rc-webui-unit-final.log`、`/private/tmp/rc-webui-docker-e2e-delivery.log`。

10 分钟测量后只修正窄窗口固定输入框和预览退出期限，编码/发送路径保持相同；最终镜像重新通过上述浏览器流程。真实相机、机器人硬件、目标真机负载及真实 OpenAI 兼容供应商尚未验收；本地 mock 与 ROS 仿真不替代这些验收。

## LLM 供应商文件与实例切换（2026-11-02）

主机完整套件（Python 3.11）1086 passed、17 skipped；ROS 2 Humble 完整套件（Python 3.10）997 passed、19 skipped。两个 LLM Python 3.11 运行镜像均从最终锁文件构建，并在禁网容器内通过实际 Compose 源码挂载初始化，覆盖方向策略四份、失败检测两份 YAML 配置。

本地 HTTP 验证了切换后第一次请求的接口、模型及凭证。重复实例链覆盖校验失败保留旧组、同路径重读、成功后暂停、显式保存到来源目录。跨容器失败检测使用 mock 机器人，4 次本地 HTTP 请求后正常退出，退出码为 0。

两套测试各有 13 条 Pillow 弃用告警。Linux 的两条录制器强制退出用例另有线程告警；在起始提交 `689822c` 和本次代码上单独复测，两项均通过且未重现告警。


## 2026-11-05 WebUI 连接宿主机 RC

生产 WebUI 新增 `RC_WEBUI_UPSTREAM`，默认容器服务名保持动态 DNS；真机目标从 `/etc/hosts` 解析 host-gateway IPv4。未修改 RC/SDK 控制语义。WebUI 67 项测试与构建通过，实际固定 njs/Nginx 配置和 Docker 网络用例 17 项通过；旧镜像在宿主机连接用例中返回 502，新镜像在默认 bridge 和 host 上游下均通过状态、MCP 请求、三路 multipart 及认证头转发验证。默认与真机 Compose 合并检查通过。

Python 3.11 example 基线 78 通过、1 项因缺少 ROS 环境跳过（不计通过）；example Docker 验收 7 项通过，容器和卷清理确认。故意注入的丢失 create 回执触发强制清理并保留 RC exit 1，属于该用例的预期断言，验收脚本退出 0。Docker 证据来自 OrbStack 的 Linux ARM64 引擎，未运行真机、厂商驱动、真实供应商或图像帧率验收。初次本地端口权限失败和 njs 默认参数语法失败均已修正执行方式或代码后重跑，未计为通过。

## 2026-11-05 WebUI AI 上游多级证书链校验深度

真机部署机（10.6.3.69）WebUI 发送自然语言消息返回 502，而 `/health`、`/preview/status` 与三路图像正常，说明 RC 代理链路可用、故障只在 `/ai/chat/completions`。Nginx 日志为 `upstream SSL certificate verify error: (20:unable to get local issuer certificate)`，上游 `api.tensoris.ai`。宿主 curl 与容器内 wget 均可完成 TLS 握手，容器信任库包含 ISRG Root X1/X2，因此不是 CA 缺失；`openssl s_client -verify_depth 1` 在该链（leaf → YE2 → Root YE → ISRG Root X2）上复现 `certificate chain too long` 与 `unable to get local issuer certificate`，`-verify_depth 2` 验证通过，确认为 Nginx `proxy_ssl_verify_depth` 默认 1 级对 Let's Encrypt 新层级过浅。

生成的 AI 代理配置增加 `proxy_ssl_verify_depth 4` 并补充单测断言；未修改 RC/SDK 控制语义，监控与 MCP 转发路径不变。WebUI 67 项单测与前端构建通过（2026-11-05）。部署机需重建 WebUI 镜像后复测自然语言发送；本轮只读检查没有在部署机启动、重建或修改任何容器，现场复测结果未计入通过证据。

## WebUI 流式 AI、玻璃交互与暂停／恢复（2026-11-05）

同源 AI 请求改为 SSE 流式接收，支持真实思考字段与指令说明预览；仅完整 `[DONE]` 和有效白名单 JSON 可以交给执行器。开发代理逐段转发，生产 Nginx 关闭缓冲／压缩并保留固定上游、私有鉴权、TLS 和错误脱敏。悬浮历史保存本页最近 50 次请求，取消、迟到响应、确认结果和各阶段光效分开处理。顶部新增直接执行的暂停／恢复按钮，在两种 LLM 模式中均可用。部署只需重建 WebUI，配置字段及 RC/SDK/policy 契约不变。

- 全部 WebUI 单测 **99 passed**，最终类型检查及生产构建通过。覆盖 Unicode／转义分片、思考别名、选择隔离、未结束／截断／拒答、长度限制、超时取消、代理首段和客户端断开，以及历史、回执和手动暂停优先级。日志 `/private/tmp/rc-webui-stream-unit-complete.log`、`/private/tmp/rc-webui-stream-build-complete.log`。
- Chromium 开发模式完整浏览器套件 **20 passed，48.2 秒**；真实生产 Nginx + mock RC + 动态 example 容器完整套件 **20 passed，约 1.1 分钟**。覆盖 example 自链、原直接命令批次、新流式和恢复语义、两种模式的暂停／恢复及移动布局。日志 `/private/tmp/rc-webui-stream-toolbar-browser.log`、`/private/tmp/rc-webui-stream-complete-browser.log`。
- 固定镜像 njs/Nginx 配置和真实网络检查 **18 passed，6.43 秒**。新增生产首段测试通过独立 gate 保证：客户端读到首段后，上游才获准发送 `[DONE]`。日志 `/private/tmp/rc-webui-stream-nginx.log`。
- 项目默认 `python3 scripts/test_scope.py`（现有 Python 3.11 测试环境）**78 passed、1 skipped，58.47 秒**；跳过原因为宿主缺少 ROS/rclpy，不计通过。日志 `/private/tmp/rc-webui-stream-example-scope.log`。未启动其他业务策略或真实硬件，未调用真实模型供应商。
- 已检查桌面 1440×900、手机 320／390 和平板 768 宽度截图；末轮提高辅助文字对比度后，再完成 **4 项定向浏览器验收**及构建。截图 `/private/tmp/rc-webui-stream-visual-final/`，日志 `/private/tmp/rc-webui-stream-visual-final.log`。系统减少动态效果时旋转停止，直接模式无 AI 历史区。

检查中修正了：手动查询的迟到回执覆盖新请求光效、待确认时只读查询的历史归属、窗口尺寸变化误判为手动回看、展开回执后新请求输出被隐藏。生产浏览器首轮存在一个测试时序问题：点击查询后立即读取了旧文案，而页面快照已确认回执；改为等待异步渲染结果，最终完整套件通过。主动取消按本次设计使用独立取消状态，旧“必须为红色错误”断言相应更新。

验证使用现有 Playwright；当前会话没有 Browser 插件，匹配浏览器下载在临时目录。首次环境运行缺少子进程 PYTHONPATH 和浏览器，均作为失败记录，修正后重新验收。自审确认没有修改控制／租约契约，没有自动重试写操作，没有把思考或半份指令当作执行结果。

两个隔离生产部署均以清理退出码 0 结束，容器、policy 实例和卷全部清空；临时 WebUI 镜像已移除，Docker 恢复原先停止状态。清理证据 `/private/tmp/rc-webui-stream-cleanup-final.json`，未修改用户私有配置或替换原有标准镜像。

## WebUI 动态 MCP 助手与 effort（2026-11-05）

在流式界面和暂停／恢复基础上，改为每次刷新能力目录、按真实结果逐轮调用一个工具的助手。增加近期对话、独立六部分提示词、原生参数校验、两类回执和可选 effort；七个 MCP 写工具补齐可选控制代次，终态操作记录不可变完成代次。此次需要同步更新 RC 与 WebUI，SDK/policy 公开契约不变。

- WebUI 完整单测 **126 passed**，类型检查和生产构建通过。日志 `/private/tmp/rc-webui-agent-unit-verified.log`、`/private/tmp/rc-webui-agent-build-verified.log`。覆盖动态描述、类型／实例校验、上限、结果节选、上下文、effort、流式异常、取消和未知受理，不调用真实模型。
- 开发代理完整 Chromium 套件 **27 passed，约 1.1 分钟**；生产 Nginx、mock RC、真实动态 example 容器完整套件 **27 passed，约 1.6 分钟**。日志 `/private/tmp/rc-webui-agent-dev-final.log`、`/private/tmp/rc-webui-agent-production-final-browser.log`。验证配置查询→接管→policy 命令→查询核实、链应用后使用新实例、completed/terminated、刷新恢复任务回执、无图像运动与反馈、异常响应不重发，以及既有流式、直接批次、监控和工具栏。
- 最后收紧目录完整性检查后，开发与生产分别补跑 **7 passed**（21.5 秒、33.5 秒）。初次误把 SDK 明确允许的 `timeout_s=-1` 当作非法，真实浏览器验收暴露问题；修正为接受 -1、拒绝缺失字段，并增加回归。最终日志 `/private/tmp/rc-webui-agent-description-verified-dev.log`、`/private/tmp/rc-webui-agent-description-verified-production.log`。
- 真实固定 njs/Nginx 的配置、effort、网络、流式首段、固定上游与错误脱敏套件 **29 passed，9.35 秒**。日志 `/private/tmp/rc-webui-agent-nginx-delivery.log`。新增 effort 用例首次缺少旧配置必填项，补全测试配置后通过；失败不计通过。
- 受影响 MCP、控制代次、运动、策略命令／重载、业务结果、直接批次和日志专项 **145 passed，70.90 秒**。日志 `/private/tmp/rc-webui-agent-backend-expanded.log`。默认 `python3 scripts/test_scope.py` 使用 example 和 example 自链：**78 passed、1 skipped，56.31 秒**；宿主缺少 ROS/rclpy 的跳过不计通过，日志 `/private/tmp/rc-webui-agent-scope.log`。

自审修正了两个结果边界：手动查询到 terminated 时保持中性、不闪绿；缺少结构化内容的写响应按受理未知保留锁定，不当作拒绝并自动解锁。原始回执、失败原因和迟到响应保护仍由同一执行器负责。已检查桌面与手机截图，产物保存在 `/private/tmp/rc-webui-agent-production-final-browser/`。

验收使用本地 API 替身、隔离测试配置和 mock 机器人，没有访问真实模型、厂商驱动或硬件，没有改用户私有凭据。临时部署清理证据位于 `/private/tmp/rc-webui-agent-production/cleanup.json` 与 `/private/tmp/rc-webui-agent-production-final/cleanup.json`；没有提交任务账本或运行产物。

两个临时项目均清理退出 0，容器、policy 实例与卷为空。测试镜像已移除，Docker 恢复初始停止状态，汇总 `/private/tmp/rc-webui-agent-cleanup-final.json`。改动保留在原工作区，未提交或推送。

## WebUI policy 等待选择与人工工具栏修正（2026-11-05）

提示词默认使用普通 `return(wait=false)`，只有当前实际运行的 policy／链成员明确会回传本次任务结果且用户需要该结果时才选择 `return_and_wait`。明确区分归还回执与业务终态，说明 `timeout_s=0` 仍创建业务等待任务。暂停／恢复／复位按钮改为共用执行器的单行 `tui_command`，不自动 take；按运行状态选择暂停或恢复，保留原提交代次、忙碌与待确认保护。

沿用用户“不重新测试”的要求，本轮未运行单测、浏览器或后端测试，也未启动 Docker 或连接模型／硬件。已更新提示词、普通归还回执、两种控制归属／LLM 模式的三个按钮及迟到模型响应的回归用例，尚未执行，不能计为通过。`npm run build`（含 `vue-tsc --noEmit`）通过，日志 `/private/tmp/rc-webui-human-toolbar-build.log`；`git diff --check` 通过。RC、SDK 和 policy 实现未改变。

## WebUI 日志、单图与浏览器视频导出（2026-11-06）

新增本页完整 AI 日志 JSON 下载、三个监控子路由、每路固定 30 fps VP8/WebM 下载及日志折叠按钮。图像接收和编码在浏览器 Worker 内执行，数据按页面会话分块存入 IndexedDB；RC、SDK、policy 公开契约和控制行为不变，部署仅需更新 WebUI。

- WebUI 全部单测 **157 passed**，类型检查及生产构建通过。覆盖完整日志不受展示条数限制、原始模型片段和无效尝试、HTTP错误、迟到响应归属、取消前保持连接、原请求的回执恢复关联、MJPEG任意分片、30fps时间轴、后台缺帧与网络突发、导出文件分块和头部回填。日志 `/tmp/rc-webui-reviewed-unit.log`、`/tmp/rc-webui-delivery-build.log`。
- 完整 Chromium 浏览器回归 **35 passed**，覆盖实际视频播放／拖动／重复导出、缺帧时保持旧画面、挂起恢复、单图路由、缓存隔离、存储耗尽后的部分导出、不支持编码时继续监控、执行中的日志快照和折叠状态，以及原有 AI／直接命令／控制回执流程。初轮已有一次在源码热更新时导航重置造成的失败，固定代码后完整重跑通过；失败不计通过。桌面与320／390／768宽度截图已检查，最后日志关联修复后再次完整回归 **35 passed，1.7分钟**；交付截图与视频证据 `/tmp/rc-webui-delivery-browser`，日志 `/tmp/rc-webui-delivery-e2e.log`。
- `.venv/bin/python scripts/test_scope.py` 默认 example 和 example 自链回归：**78 passed、1 skipped，64.54秒**。跳过项需要 ROS 2/rclpy 及消息包，不计通过。日志 `/tmp/rc-webui-example-scope.log`。
- 固定生产构建、同机单前台 Chromium156.0.8078.4、三路30Hz mock源，连续 **600.0092秒**同时显示和录像：左腕 **29.9029fps**、头部 **29.8979fps**、右腕 **29.9029fps**，均超过29.5fps要求；显示延迟P95为24.3～26.0ms，浏览器异常0。采样最大编码／存储积压6帧；主页面JS堆8.05～11.90MB，临时浏览器存储最终约229.04MB。JS堆指标不代表浏览器全部进程内存。结果 `/tmp/rc-webui-recording-bench/results/result.json`。
- 三个实际视频经ffprobe读取全部帧：左腕18007帧／600.233秒、头部18241帧／608.033秒、右腕18473帧／615.766秒；均为VP8、640×480、30/1fps，时长与帧数误差小于2ms。文件按各自点击时间截止，因此依次下载的视频时长递增。视频和截图位于上述结果目录。

验收使用本机隔离mock RC、example worker和API替身，没有连接真实模型、厂商驱动或机器人。本轮未执行Docker/Nginx容器或真机验收；静态构建验收不替代这些环境。启动浏览器夹具时需让 `PYTHONPATH` 包含本仓库 `src` 与 `packages/policy-sdk/src`，并使用匹配当前Playwright版本的Chromium。

本次独立性能夹具以退出码0完成统一清理，静态测试服务已停止；浏览器回归夹具正常退出，测试端口已释放。未提交或推送代码，任务账本和运行产物均未纳入版本控制。

## TUI 命令树与 policy configure（2026-11-06，RC/SDK 0.14）

命令、帮助、补全和文本批次共用正式命令定义；旧语法不再作为执行别名。RC 配置通过草稿保存后重启生效，全部内置策略通过 `configure(context, config, key, value, callback)` 解析业务与执行设置。协议覆盖候选准备、应用、确认和受代次保护的发布；普通更新保持引用，reset 与原进程 reinitialize 完成后暂停。详细语法见 [统一命令树](commands.md)。

验证结果：

| 范围 | 结果 |
|---|---|
| 宿主 Python 3.10：`python3 scripts/test_scope.py --interface` | **1582 passed、76 skipped**；13 条既有 Pillow 弃用警告 |
| Linux / ROS 2 Humble：example ROS、配置协议及 MCP 专项 | **34 passed、4 skipped**；真实 rclpy example 模拟器通过，4 项旧 Conda 开关用例未启用 |
| 真实 Docker：example 与 example 自链 | **7 组检查通过**；新增 ordinary/reset/reinitialize 配置证据 |
| WebUI 单元测试 | **174 passed** |
| WebUI 类型检查与生产构建 | 通过 |
| RC/SDK Python 3.10 语法与 diff 空白检查 | 通过 |

独立审查发现并修复了配置发布后人工 pause 被成功收尾覆盖、批次额外解析 policy 原始文本、实例保存回退到未运行来源三个问题，均先复现失败再验证修复。另覆盖内部操作名称不能被自定义命令拦截，以及 RC/global JSON 预检与 policy 原始文本解析的区别。运行时 pause 使受理代次失效时，尚未开始的 configure 取消并保留旧有效配置。

Docker 中 RC 使用 Python 3.10，example 使用 Python 3.14.9。新配置路径只递增目标及逻辑链版本，其他实例保持不变；reinitialize 保留容器、host、执行 PID 与模块并更换 context。本次部署的容器和卷已清理。退出故障注入中发生的强制清理继续准确记录非零退出码，不作为普通安全退出。

证据：`/tmp/rc-tui-interface-verified.log`、`/tmp/rc-tui-linux-ros.log`、`/tmp/rc-tui-example-docker-verified/{result.json,container.log,cleanup.json}`、`/tmp/migration-webui-direct-final.log`、`/tmp/migration-webui-build-final.log`。本轮未调用真实模型供应商、GPU 模型或机器人硬件；跳过不计通过。

## RC/SDK 0.15 输入辅助验收（2026-11-07）

范围为历史虚影、显式确认的候选菜单、静态参数/config_hints、只读容器路径查询及能力元数据；manifest 保持 schema 3。新版不改变注册命令的执行类型约束，policy 配置仍接收原始值。九个内置来源的示例清单均提供提示。

专项覆盖：

- `test_argument_hints.py`、`test_builtin_completion_hints.py`：旧参数声明兼容、元数据校验、能力查询保留基础 args、九来源提示。
- `test_completion_engine.py`、`test_completion_service.py`：不完整输入、引用与中文、声明顺序、raw 值无额外空格、异步身份失效、超时、缓存和慢来源扫描。
- `test_path_completion_host.py`：真实 host 查询、root/符号链接限制、目录/后缀/点文件、实例绑定、慢扫描期间心跳与退出、有界待查请求和迟到响应。
- `test_command_history.py`、`test_history_secret_envelopes.py`：跨进程保存、去重、隔离、0600 原子写入、损坏文件、敏感批次封装、最后一次提交保存和卡死文件系统退出期限。
- `test_tui_input_assistance.py`、`test_completion_terminal.py`：真实 Textual 和 POSIX PTY；Tab/Space/Enter 两阶段确认、ghost 的 Right/Esc、方向键与窄布局、快速 Ctrl+U 后输入或粘贴、真实非空提交打断批次、候选确认不打断批次。

Docker 使用 `python3 tests/example_docker_smoke.py --evidence-dir /tmp/rc015-example-docker`，完整 8 组通过。测试为两个 example 实例挂载 RC 无法访问的 `/completion-only` 目录，逐个 host 查询，验证只读挂载、文件名规则、中文空格路径、手动字符串不受建议限制，以及容器/host/PID/context/版本保持独立。环境为 RC Python 3.10.12、example Python 3.14.9，最终清理报告无遗留容器或卷。故障注入产生的强制退出非零事实按原契约保留。

Linux/Humble Python 3.10 中，元数据、host、补全分析/服务与 example 真实 rclpy 仿真专项 79 passed；WebUI 174 passed，类型检查和构建通过。无真实模型、GPU 或硬件验证，不能用 mock 或跳过项替代这些验收。

独立 Astra 审查并修复敏感历史封装漏检、无效本地提交的批次打断回归、慢来源扫描冻结实例元数据，以及原始值末尾空格。引号规则按 Python shlex 对照，14,421 个合法字符组合及 49 组插入往返一致；后续参数保持原样。

最终冻结代码执行 `python3 scripts/test_scope.py --interface`：**1706 passed、76 skipped**，584.83 秒；13 条 Pillow 弃用警告及 1 条 multiprocessing resource_tracker 重启告警。跳过项不计通过。最终日志为 `/tmp/rc015-interface-complete.log`；此前因修复而中断的两轮不计完整验收。

其他证据：`/tmp/rc015-example-docker/{result.json,container.log,cleanup.json}`、`/tmp/rc015-linux-ros.log`、`/tmp/rc015-webui-test-verified.log`、`/tmp/rc015-webui-build.log`、`/tmp/rc-input-ui-final.log`、`/tmp/rc015-path-quotes-green.log`。真实 PTY 已验收键码、中文输入、候选布局及方向键；桌面终端窗口的人工视觉复核因原生界面服务不可用未完成，不能用 SVG 导出代替该项。

## 2026-11-13 history 调度、模型配置导出与保存

本轮按授权只运行定向检查，未运行全量 `test_scope.py`、完整接口回归或 Docker smoke。RC 的同步目标提交现在等待完整发布确认后再进入下一目标；受保护尾目标不会被重复推理请求阻断。三个 history 来源支持实例内存模型快照、`model_config.*` 配置与原模型 JSON 保存；流水线融合改为 SDK 接近点拼接，全部 14 维参与。

- RC 调度 RED 为两个可复现断言失败，修复后控制器与 connector 定向检查 **22 passed**。
- 导出协议、保存回滚与配置事务检查 **39 passed**；example 自链/实例保存集成检查 **17 passed**。
- 集中定向集：**271 passed、2 skipped，78.73 秒**。包括 RC 控制器、SDK 配置事务、导出与文件回滚、example 配置和自链、MCP 配置、SDK 接近点黄金样例、原 history/LIFT2 配置与生命周期。两项跳过为本机缺少 Torch 的 LIFT2 CPU 变换用例，跳过不计通过。
- 相对来源路径和公开配置展示的收尾修复后，原 history/LIFT2 设置、真实测试 worker、example 配置/实例保存/MCP 的定向回归 **56 passed，31.76 秒**。
- `qwen35_history_clip` 仅作源码/配置静态核查，未新增或运行其测试；未加载真实 GPU 模型或连接机器人。普通 reset 保留裁剪累计计数，实例重新初始化/重载清零；计数随后续策略消费反馈更新。

日志位于 `/private/tmp/history-control-config-final.log`、`/private/tmp/history-path-view-green.log`，RED/GREEN 分阶段证据位于同目录的 `history-*.log`。真实配置继续被 Git 忽略，原有 pipelined/replan 值保持不变。本轮没有服务器操作、部署、重启、提交或推送。

后续补充：clip 新增 `params.arm_clip_enabled` 布尔热开关，旧配置默认开启，monitor 显示开关状态。关闭期间继续跟踪已消费指令，重新开启时沿用最后已消费目标；关闭期间产生的动作不累计硬裁剪次数。按用户明确要求，此补充直接实现，未新增或运行测试；上述通过结果不作为此开关的测试证据。
