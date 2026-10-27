# 统一 Policy 配置与一键 Setup 实施计划

用户已批准聊天中的完整方案（2026-10-26）。两个仓库独立实现，不跨仓库导入。

## 约束
实际 RC/configs/policy JSON 改为本地文件，提交 json.example；现有值优先深合并，列表/空值保留，校验后原子写、私有备份。LLM api_key 直接配置，不回退环境；公开输出脱敏，在线密钥 set 在记录前拒绝。Qwen deployment 冷配置生成独立环境/Compose覆盖；同host环境冲突拒绝。setup默认RC+hold，可选LLM/Qwen；config-only不安装；不启动控制器、不安装系统组件/ROS/驱动，不下载权重、不调用模型API。RC.env职责保留。

## 执行任务
1. LLM配置与公开输出：api_key迁移、取消环境透传、脱敏与命令拦截；回归真实HTTP/worker、TUI、MCP、保存原值。
2. 配置文件与部署基础：stdlib合并/校验/原子文件、备份和dotenv处理；production模板转换保留实际文件；Qwen deployment及独立环境校验。
3. Setup与Compose入口：按需Conda/pip或镜像构建、幂等身份/目录、生成部署覆盖和选择记录、各overlay兼容、忽略及构建上下文。
4. 文档与验收：全套测试、干净复制setup、实际隔离容器/可用Conda验证、最终独立审查。

## 验收重点
密钥不得出现在公共查询/日志/历史/快照/Compose文件/镜像。保存配置保持原值。现有配置/注释/多行dotenv保留。新clone可setup、重复运行幂等、仅所选依赖安装，共用Qwen只准备一次。修改部署后重新setup并重建运行组。同host不同key可运行，进程级配置冲突拒绝。
