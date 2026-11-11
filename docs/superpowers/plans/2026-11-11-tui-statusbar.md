# 精简 TUI 固定状态栏

用户批准从 develop@8c1e096 新建 `codex/tui-statusbar`，复用当前目录，保留私有配置；不自动提交、推送或远端部署。实现与独立审查使用 gpt-6-astra / max。进度与验证证据只记在忽略目录 `.codex/task-state.md`。

## 固定显示

状态栏按下列顺序显示，继续使用 ` | ` 分隔和窗口自然换行：

1. `state`
2. `policy`
3. `control_owner`
4. `in_flight`
5. `consumed`
6. `请求 N：裁剪 X，缓存 Y`
7. `runtime`（生命周期阶段）
8. `target`
9. `runtime_error`
10. `recording_state`
11. `recording_error`
12. `connector_source`
13. `connector_config`

所有显示项固定保留。缺失、None 和空字符串显示 `—`，False 和 0 正常显示。没有有效请求摘要时显示 `请求 —：裁剪 —，缓存 —`。错误中的换行整理为空格，保留完整路径和错误内容；其他字段从常驻栏隐藏。

## 数据和查询

新增 RC 内部只读状态栏快照，在锁保护下获得一致的运行状态及连接元数据，TUI 只格式化内存数据。`connector_source/config` 来自当前有效绑定的 `ConnectorRobot.spec`，config 使用原始配置路径字符串，不读配置内容，不重新读取磁盘 profile。

初始未连接、断开和新连接失败显示空值；旧绑定仍有效时保留旧值，新绑定提交后更新两项。新连接成功但 policy 适配失败仍显示新连接。测试对象缺少 spec 时安全显示空值。

现有 `status()`、MCP、SDK 返回结构和行为保持不变。`status` 可查询全部字段，`connector status` 保留完整连接状态，`record status` 保留完整录制信息。更新三条对应帮助和命令文档，说明查询入口。

## 验证

- 固定白名单、字段顺序、空值占位、False/0、请求摘要失效和错误换行。
- 状态栏隐藏字段仍通过真实查询命令完整返回；公共返回结构不增加 UI 字段。
- 有效绑定、切换前后、预检查失败、断开后连接失败、policy 适配失败、缺少 spec；磁盘 profile 修改不影响已绑定显示。
- 真实 Textual 宽窄窗口、长路径和完整错误文本，UI 路径不触发文件/socket I/O。
- 增量运行相关 TUI、状态查询、connector 专项；阶段结束运行 `python3 scripts/test_scope.py`，使用 example 和 example 自链验收，不启动其他策略。
