# MaaS JPEG / YAML Implementation Plan

> 使用 superpowers:executing-plans 在当前会话单智能体实施；用户要求多智能体须先确认模型调度，因此本任务不派发代理。

**Goal:** 从 fd98f27 支持可配置 JPEG 发送，并将默认 MaaS 模型配置迁移到 YAML。
**Architecture:** 在 MaaS 网络后端编码 JPEG，raw/状态/动作沿用现有 codec；安全配置加载独立于 RC。
**Tech Stack:** Python 3.11, NumPy, Pillow 12.3.0, MessagePack, websockets 15.0.1, ruamel.yaml 0.19.1。
**Spec:** docs/superpowers/specs/2026-11-10-maas-jpeg-yaml-design.md

## Global Constraints

- 当前 checkout、新分支 codex/maas-jpeg-yaml、基点 fd98f27。
- 不改 RC↔SDK 契约；唯一进度账本 .codex/task-state.md。
- 不操作真实 MaaS 或硬件，不提交私有配置、.codex 或运行产物。
- 保留切换旧提交后出现的既有未跟踪 configs/robots/、connector/。

## Review Focus

- YAML 异常/警告不得泄露 API key；布尔值不能充当 quality。
- 旧 JSON 与自定义配置路径继续可加载。
- JPEG 质量配置改变实际编码，解码后的颜色次序、补边、尺寸正确。
- raw 模式完全绕过 JPEG 编码，图像租约释放规则不变。
- 新 YAML 与旧 JSON 私有文件不会被模板复制或 Git 收录。

## Task 1: YAML 配置和部署

**Files:** configuration.py、MaaS model/policy 模板、requirements/pyproject、scripts/config_files.py、tests/project_templates.py、MaaS 配置/部署/worker fixtures。
**Interface:** `load_model_config` 返回 observations.jpeg(false)、jpeg_quality(85) 和现有字段；支持 YAML 与旧 JSON。

- [x] 补 YAML 块文本、重复键、非有限值、标签/别名、质量边界和私密值错误测试；补 setup 模板生成/权限/幂等测试。
- [x] 运行配置/部署专项，确认新功能用例失败。
- [x] 实现安全 YAML 读取、严格字段校验、模板/路径/依赖锁更新；保留 JSON 兼容。
- [x] 更新 fixture 为 YAML，验证配置/部署和 setup 相关测试。

## Task 2: JPEG 编码与验证

**Files:** images.py、backend.py、MaaS images/backend/timing/worker 测试、使用及验证文档。
**Interface:** `encode_jpeg(frame, quality) -> bytes`；网络消息 images[role] 为 enc/data 或原 ndarray。

- [x] 补 JPEG 实际解码、质量差异、三相机握手/载荷/统计和 worker 释放测试。
- [x] 运行相关用例，观察预期失败。
- [x] 实现按配置编码与握手声明，统计实际字节和编码耗时。
- [x] 专项通过后运行 `python3 scripts/test_scope.py --policy internvla_maas` 和受影响 setup 测试。
- [x] 检查可用 Docker 环境并运行隔离验证；记录未验收的环境边界。
- [x] 自审完整差异；迁移当前私有模型配置并验证忽略规则，记录证据。交付分支改动，不推送。
