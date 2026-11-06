# 项目约定

默认中文交流。独立项目，不从原 robot-control、deploydev 或训练仓库导入。

- 主容器为 ROS 2 Humble / Python 3.10，hold 为 Python 3.14，两个 Qwen 共用 Python 3.11 镜像、分别运行独立容器；不使用 Conda。其他 policy 的 Python 与依赖版本由对应镜像锁定。
- 每个可执行 policy 实例独占一个容器、host 和执行进程；原生 chain 自身不增加容器，每个环节独立实例化。重复策略按出现顺序命名为 policyName-0/1/2，来源目录与镜像保持共用。显式 switch 和 reload_all_policy 在整个旧组退出证明齐备后重建全组。reload_policy 仅重建命令目标实例，先证明其进程树、容器及外部 reader 已退出。其他成员的在途共享图像必须独立确认释放。
- SDK 仅依赖 NumPy 与 posix-ipc，不引入 ROS/TUI/模型框架。
- `reinitialize_policy` 在原执行进程和已加载模块中交接目标 context。先暂停并收齐读者确认，再在旧绑定上 release、新绑定上 initialize。初始化或提交失败只能手动冷重试该实例。普通动作请求摘要进状态栏，逐条输出使用 TRACE。
- 只有控制线程发送动作并修改缓存；成功发送完整目标才消费。UI 不执行 socket I/O、推理或生命周期操作。
- RC 参数冷改：`config rc set` 编辑草稿，`config rc save` 落盘、重启生效。`policy <实例> config set` 的设置项与原始值全部交由 policy 的 `configure(context, config, key, value, callback)` 解析，候选仅可修改 params/execution；callback("reset"/"reinitialize") 延迟执行。旧自定义命令 update_config effect 继续使用 manifest 白名单叶字段。普通提交保持配置引用不变，版本两端同时确认。
- MCP 默认只读；录制开关是明确例外，无需接管且不改变运动、控制权或 policy 等待任务。链应用/保存、策略命令和重试需先 take，完成后保留 MCP 控制权并暂停 policy。接管、归还及生命周期提交保持代次复核语义。policy 故障不抢夺 MCP 已持有的控制权。
- 人工文本入口 `tui_command` 是另一明确例外：无需 take，按 TUI 语义执行；Agent 应使用原结构化工具。仅文本批次 FIFO 排队；真实本地 TUI 的非空提交清除批次未开始的行及等待批次，已开始的行按原生命周期收尾。禁止递归 MCP 和本地面板命令；HTTP 断连不取消批次。
- 图像为只读 POSIX 共享内存；租约只有明确释放确认或旧执行进程树退出证明才可回收。断连不是证明。
- 基础 Compose 只含 RC 与 ipc-anchor；策略配置归策略目录，目录名为来源身份，运行实例另有唯一身份，使用 schema 3。实例命令、monitor、退出证明不得按来源合并；保存必须指定明确实例并写回来源目录。RC 生命周期线程管理动态容器，UI 不运行 Docker。
- 退出/信号走统一绝对期限清理；强制清理返回非零。假 ROS/mock 不代表真机验收。
- 厂商备份保留原样，只构建 ros_ws 下的消息包；不自动启动厂商硬件驱动。

长任务在忽略目录 `.codex/task-state.md` 维护唯一账本与验证证据。用 rg 搜索，第三方 API 先查 Context7（无条目时查官方来源）。平时增量验证，阶段边界跑所选范围的完整套件：

- RC 内部调整且对策略可见的接口/语义不变：`python3 scripts/test_scope.py`。用 example 和 example 自链验收，按改动补跑对应 RC 单测；不启动其他策略。
- 更新策略自己的实现、配置、镜像或依赖：`python3 scripts/test_scope.py --policy <来源名> ...`，共享实现改动选所有受影响的来源。
- 更新 RC↔policy/SDK 公共契约（含动作/观测、协议、配置、租约、生命周期和结果语义）：同步补 example 用例，`python3 scripts/test_scope.py --interface` 运行完整回归。Docker、ROS、真实模型及硬件按各自环境验收，跳过不计通过。见 `docs/verification.md`。

不提交 .codex 或运行产物。
