# Connector 命名统一与帮助系统实施计划

已批准目标：在 `codex/connector-commands-help` 分支上完成 camera 默认切换、机器人连接命名统一及统一帮助展示。基线为 `28b1c5e`。复用现有目录，保留私有配置，不提交、不推送、不部署远端。实现及独立审查子智能体统一使用 gpt-6-astra / max。

## 约束与接口

- 裸 `camera` 等价 `camera toggle`，仍仅在本地 TUI 操作面板。
- 机器人连接命令统一为 `connector list/status/switch/retry`；MCP 工具为 `connector_profiles`、`switch_connector`、`retry_connector`。
- 对外状态、图像、录制及 SDK 能力统一使用 connector 命名，包括 `connector_generation`、`execution.on_connector_switch`、`connector_switch_v1`。模型推理、构建、图形后端不改。
- Profile 的 `source` 是来源，`connector` 是实例；删除冗余来源字段 `backend`。修复列表完成后没有终端输出的问题。
- 不提供旧名称别名、配置兼容或自动迁移，旧键及引用直接报错；不新增兼容/迁移测试。RC 与 policy SDK 为 0.17.0，保持 schema 3。
- SDK 不增加 UI 依赖，帮助查询不加载模型。生命周期、控制权、图像租约及退出证明保持现有语义。

## 帮助接口

保留简短 `help` 字符串和现有参数声明。在 policy 顶层、每条 `tui_commands` 及 `config_hints` 支持可选 `help_details`：

```yaml
help: 设置任务消息
help_details:
  group: 任务设置
  description: 修改当前实例的任务消息，保留执行进程。
  sections:
    - title: 执行影响
      body: 配置提交成功后生效。
  examples:
    - title: 设置中文消息
      command: 'policy <实例> set-message "整理桌面"'
```

字段可省略；只接受普通文本，拒绝未知字段或错误类型，不执行回调、表达式或富文本代码。`<实例>` 是唯一实例占位符，已加载实例由 RC 填充，未加载保留提示。参数表从 args/config_hints.settings 生成。重复来源按独立实例展示。

统一的可序列化 HelpDocument 表达标题、摘要、章节、目录、参数和示例。日志保留无 ANSI 的完整纯文本 message，并可附加 help_document；TUI 在现有后台格式化线程内渲染，帮助默认展开。复制使用逻辑文本，软换行及装饰不能污染结果。窄窗口采用纵向参数布局。

覆盖 `help`、`help <完整命令路径>`、`policy help`、`mcp help` 及 HTTP MCP 帮助。总览按功能分组，详情展示用法、参数、示例及执行影响。MCP 参数从 schema 获取类型、必填、默认值、枚举和嵌套结构；send_command 等工具补充语法和示例。HTTP 帮助返回可读 content.text 和结构化数据，其他工具结果不改变。RC `--help` 继续使用 argparse，整理中文分组。

## 实施顺序

1. 命令及全链路连接命名：包含配置校验、模板、WebUI 提示、使用文档和既有测试更新。
2. 静态帮助内容及通用投影：SDK 校验、RC 文档汇总、终端/MCP 入口、example 示例。
3. 日志区富排版及 CLI：处理中文、窄屏、异步重排、滚动、复制；完成相关渲染测试。
4. 独立审查、修复、完整回归和验收证据。

命令解析、controller、terminal_commands、mcp_server、SDK 配置及 example 模板由阶段一交接至阶段二，避免并行编辑。唯一进度账本为忽略目录中的 `.codex/task-state.md`。

## 验证

- 增量测试验证 camera 连续切换、connector 列表输出、新 MCP 接管/代次/切换/重试、图像代次、录制分段。
- example 与 example 自链覆盖 reset、同进程重新初始化及失败实例恢复。
- 帮助覆盖未加载策略、无活动策略、重复实例、参数与示例、MCP 双投影；查询不得启动策略。
- 实际 TUI 验证中文、窄屏、缩放、滚动与复制，headless 验证完整纯文本。
- 阶段边界执行 `python3 scripts/test_scope.py --interface`，并执行 WebUI 测试和生产构建。
- Docker 可用时运行隔离 example 验收，保存清理证据；ROS、真实模型、硬件未执行明确记为未验证，跳过不计通过。
