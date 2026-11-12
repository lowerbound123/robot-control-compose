# llm_failure 实施计划

目标：按用户批准的异步方案实现动作透传、每60已消费步检查、最近5组图像、DeepSeek严格JSON失败判定与SDK finish回调。

规范：`../specs/2026-10-28-llm-failure-design.md`，以用户确认的异步方案为准。

## 1. 配置、历史与异步运行时

- 创建独立 `rc_llm_failure` 包，接口 `FailurePolicy(config, provider=None)`、`policy(action_chunk, images, runtime)`、`reset_context()`、`close()`。
- 内部 `Snapshot(step, images)` 为不可变编码快照；`provider.decide(task, prompt, snapshots) -> bool`。后台任务不捕获 runtime 或共享内存数组。
- 先写调度/透传/慢请求/代次/释放测试并确认失败，再实现；阈值0/59/60/119/120、busy不排队、每次回调收集结果，结束结果仅提交一次。

## 2. DeepSeek 适配器

- 新增严格布尔解析器及 `ProviderError`；真实OpenAI兼容客户端发送命名、按步数排序的图片与task。
- JSON mode、thinking disabled、temperature0、max_tokens128、max_retries0；请求内容32MiB上限；所有供应商错误脱敏。
- 本地HTTP测试先失败后实现，验证有效/非法JSON、截断、拒答、错误码、断连、超时和实际请求形状。

## 3. 策略目录与镜像

- policy/llm_failure 四个标准回调，schema3 qpos默认，可冷改epos/mixed；独立Python3.11 Dockerfile/Compose及依赖哈希锁。
- 固定SDK>=0.10、NumPy2.2.6、Pillow13.0.0、OpenAI3.37.0、posix-ipc1.3.2；不引入SciPy或模型框架。
- 配置template留空task/key，目录发现无需RC分支；真实worker测试实际策略入口与只读POSIX图像释放。

## 4. 端到端、文档与验证

- 实际host/worker/原生链/MCP测试false透传、true归还、无wait只暂停、后段错误覆盖finish、慢API不阻塞动作回调、reset不应用旧结果。
- 更新README和独立使用文档，解释每60实际动作步、后台结果在下一回调生效、reset屏障、同模式链与MCP等待。
- 阶段边界完整主机/Linux套件；独立镜像实际安装包验证与隔离跨容器测试，保留原工作区修改；证据写唯一.codex/task-state.md。
- 最终一次独立只读代码审阅；重要问题用失败回归修复并完整验证。未配置真实凭证/场景时不宣称真实DeepSeek判断准确率或真机验收。
