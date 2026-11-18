# OpenWAM MaaS · ACOne

`openwam_maas_acone` 把 ACOne 三路相机图像、当前关节位置和任务文本发给已有的 OpenWAM 服务，每次取回 32 步绝对关节目标。服务端应使用 **`arx_acone` 归一化组**。左右夹爪（下标 6、13）严格大于 `-1.7` 时改为 `0.04`，等于或低于阈值时保留模型输出。每个新预测只处理一次夹爪，随后参与融合。

## 接入现有服务

```bash
./setup.sh --config-only
```

在本策略目录的 `deployment.env` 中填写已有模型服务的 Docker 网络名 `OPENWAM_NETWORK`，在生成的 `policy.yaml` 中填写 `params.service_url` 和 `params.task`。模板对应的网络是 `openwam-main_default`，地址是 `ws://openwam:8848`。这里的 `openwam` 是模型在该网络中的服务名。容器内的回环地址指向消费者自身。协议握手不暴露归一化组，配置地址时请核对该服务使用的机型。

准备 CPU 客户端镜像后，从 TUI 选择策略。每个实例都有自己的连接和动作缓存，模型服务由现场独立管理。

```bash
./scripts/build.sh openwam_maas_acone
```

```text
policy switch openwam_maas_acone
policy openwam_maas_acone switch-task "pick up the cup"
monitor openwam_maas_acone
```

加载后策略保持暂停。使用 `policy openwam_maas_acone config save` 保存本实例的任务或参数。若同一来源在链里出现多次，使用实际实例名。

## 推理监控

在 TUI 输入 `monitor openwam_maas_acone` 可查看当前请求阶段、客户端分阶段耗时、服务端报告的延迟、动作余量，以及最近一批左右夹爪处理前后的 32 步值。等待服务响应时面板仍可刷新。字段和复位行为见[OpenWAM MaaS 监控说明](../../docs/policy-monitor.md#openwam-maas-策略)。

## 图像、动作与时序

- 初始化用 WebSocket JSON `ping` / `pong` 核对协议 v2、`qpos`、`chunk_delta`、32 步、夹爪下标和多视角。`chunk_obs` 携带原始 14 维 `state`、任务原文 `prompt` 和三张原分辨率 RGB 图像，分别以无损 PNG 的 Base64 发送到 `head_camera`、`left_wrist_camera`、`right_wrist_camera`。服务端负责缩放、拼图、反归一化和 delta 还原。
- 默认 `sync`、`call_every_actions: 1`、`params.request_when_remaining: 0`。RC 确认消费最后一步后，策略才用新观测请求下一块，等待期间由 RC 保持目标。将余量设为 1–32 可提前请求，同时最多一个在途请求。推理期间已消费的步数从新块裁去，再按 RC 实际剩余轨迹做最近点对齐和指数融合。整块过期时使用新观测重新规划。
- `params.open_timeout_s` 默认 10 秒，`params.inference_timeout_s` 默认 50 秒。提高请求期限时，为 RC 的外层推理期限留出余量。`config set params.service_url` 会重新初始化连接，修改服务网络需冷重载实例。
- 切换任务会作废旧计划。reset 清理本实例缓存，有在途请求时关闭该连接并重新握手；其他实例保持独立。连接中断、服务报错、超时或动作形状和数值非法时，策略向 RC 报错。

```text
policy openwam_maas_acone config set params.request_when_remaining 4
policy openwam_maas_acone config save
```
