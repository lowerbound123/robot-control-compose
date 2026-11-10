# MaaS JPEG 与 YAML 模型配置

基点 `fd98f27`，在当前 checkout 的 `codex/maas-jpeg-yaml` 分支实施。
用户要求支持发送 JPEG，并在 YAML 模型配置中编辑开关与质量。

## 方案与边界

采用 MaaS 交接文档已定义的 JPEG 消息：启用时 `robot_hello.image_encodings`
为 `["raw", "jpeg"]`，每路 `images[role]` 为 `{"enc": "jpeg", "data": bytes}`。
原 raw ndarray 编码继续作为关闭 JPEG 时的路径；不改公共 SDK、RC 图像池或动作契约。
Base64 和将 JPEG 字节伪装成原始 RGB ndarray 均不符合该协议，不采用。

协议依据：用户已有 `lift2-internvla-maas-handover.md` 的模型契约章节。
真实服务兼容性仍需单独验收，本地替身不代表真实模型或硬件通过。

## 配置与行为

- 默认文件改为 `policy/internvla_maas/model.yaml`，模板为 `model.yaml.example`。
- `observations.jpeg: false`；`observations.jpeg_quality: 85`，严格布尔和整数 85～100。
  默认关闭以保留已有运行行为；下限来自 MaaS 协议，100 为 JPEG 编码器支持的上限。
- 保留 224×224 双线性等比例缩放及居中补黑，随后可选 JPEG 编码。
- YAML 安全加载、重复键/非有限值/非普通数据拒绝；错误不带凭据或配置原文。
- 兼容旧 `.json` 配置；新模板和文档使用 YAML，命令参数沿用基线的字符串格式。当前私有 JSON 转换为
  YAML 并仅更新默认路径引用，原 JSON 留作备份，不打印其内容。
- 设置属于冷配置，重建实例后生效。质量参数即使 JPEG 关闭也校验。
- 增加编码阶段耗时，`last_image_bytes` 统计实际发送的图像载荷长度。
- MaaS 独立镜像增加锁定的 ruamel.yaml；SDK 依赖不变。

## 验证

测试先行：YAML/JSON 配置、非法参数及脱敏、三路 JPEG 真实解码与颜色/尺寸/质量、
握手、raw 回归、准确字节统计、真实 worker 图像释放、setup 生成及私有文件隔离。
阶段边界运行 `python3 scripts/test_scope.py --policy internvla_maas`，补 setup/YAML
相关测试；可用时运行隔离 Docker 验收，不连接真实 MaaS 或机器人。
