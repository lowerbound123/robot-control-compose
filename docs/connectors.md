# 轻量 connector

RC 通过独立 connector 容器读取设备状态、请求图像和发送目标。`acone` 封装原有 ROS 2 接口，`mock` 提供同一接口的模拟设备。RC 与 mock 镜像不包含 ROS；ACOne 使用 Humble / Python 3.10。厂商驱动仍由现场独立启动。

## 配置和启动

RC schema 3 的连接配置为：

```yaml
schema_version: 3
connector_directory: ../connector
connectors:
  body:
    source: mock
    config: devices/default.yaml
robot:
  connector: body
```

`body` 是实例身份，`mock` 是来源目录。第一版只允许一个实例；内部连接、缓存、容器和清理记录按实例寻址，来源相同不代表实例相同。相对 `connector_directory` 从 RC 配置目录解析，设备配置路径从来源目录解析；也可显式引用 RC 容器中可读的绝对配置路径。

```bash
# 默认仅构建无 ROS 的 mock connector 和所选策略。
./setup.sh --policies example
./scripts/compose.sh up -d --no-build

# 使用 ACOne 时显式构建其镜像，修改设备配置后启动。
./setup.sh --policies example --connectors acone
./scripts/compose.sh -f compose.acone.yaml up -d --no-build
```

setup 只初始化缺失的 `connector/*/devices/*.yaml`，不覆盖已有设备配置。ACOne 的 topic、消息类型、namespace、反馈超时和 `ros_domain_id` 位于 `connector/acone/devices/robot-main.yaml`。ROS 模拟器使用同目录的 `ros-test.yaml`。这些参数属于连接配置；修改后通过命名 profile 执行 `backend switch <名称>` 重新创建 connector，或重启整个部署。切换成功后 policy 保持暂停。

旧部署通过 RC 环境变量设置的 ROS 域，需要写入设备配置的 `ros_domain_id`。ROS 模拟器的 `ROS_DOMAIN_ID` 仍需与 `ros-test.yaml` 一致，默认均为 83。RMW 和 ROS 网络设置由 `connector/acone/compose.yaml` 管理，不再通过 RC 容器环境传递。

旧 `robot.backend` 配置会明确报迁移错误。迁移时将旧 `robot` 中除 `backend` 外的设备字段移入对应来源的设备 YAML，再用上面的 `connectors` 与 `robot.connector` 引用替换。也可用 `robot: {profile: acone}` 选择命名连接，具体结构与手动重试规则见 [backend 热切换](backend-switching.md)。RC 的控制频率、限速、reset 和策略配置保留在 RC YAML。不要把私有 YAML 或运行产物提交到版本库。

基础 Compose 仍只有 RC 和 ipc-anchor。RC 的生命周期线程动态创建 connector，加入 anchor 的 IPC，并使用独立的 connector RPC 卷；policy 不挂载这个调用端点。只有 ACOne connector 使用 ROS 所需的 host network，RC 保持基础网络配置。

ACOne 部署的 MCP 也通过基础 Compose 的本地端口映射访问。若旧部署为 WebUI 配置过 `RC_WEBUI_UPSTREAM=host.docker.internal:4000`，迁移时移除这个旧覆盖，使用默认的 `robot-control:4000`。

## 接口与动作语义

- `get_state(since)`：查询最新缓存及有界反馈历史；保留源采样序号、时间与错误。
- `get_images(cameras)`：将当前缓存发布到共享内存，返回描述符、元数据和租约 ID。
- `send_targets(targets)`：逐臂发送 `qpos` 或 `epos` 绝对目标，返回每臂发布结果。
- `release_images(ids)`：明确释放读取完成的图像租约；重复释放不重复减引用。
- `close()`：停止新请求，在同一个退出期限内排空已提交动作并关闭设备。

动作与查询使用不同 Unix socket。请求 ID 关联回执，不参与控制权判断。connector 按 FIFO 调用设备接口，无控制循环、动作有效期、授权心跳、插值或自动重发。`take/pause/switch` 只影响 RC 后续产生的目标；已提交的 A1、A2 仍排在新目标 B1 前执行。

RC 区分提交与确认位置，允许在等待回执时继续按配置 Hz 提交。只有完整目标的全部发送步骤确认成功，才增加 `consumed_actions`。已经提交或开始插值的前缀不会被新的异步推理结果覆盖。部分发布与结果不确定会使 RC 停止新增动作；已经提交的命令不会自动撤销或回滚。队列有界，满时明确报错，不丢弃旧命令腾位。

发布成功只表示设备发送函数成功返回，不表示机械臂到位。人工运动仍依据最终发布边界之后的新鲜反馈判断到位。RC 内部控制权和 policy 请求代次继续生效，但不会发送给 connector 用于取消旧动作。

## 图像和读者

```text
相机 → connector 内存缓存
RC get_images → POSIX 共享内存完整快照 → 小描述符
RC 只读映射并复制 → 关闭映射 → release_images
RC 自有缓存 → 预览／录制／现有 policy 图像池
```

像素不经过 socket，不编码成 Base64，也不生成传输用临时图片文件。没有 `get_images` 请求时不会发布新的共享内存快照。写完后才返回描述符；租约存在时不覆盖该槽。槽不足明确报错。

RC 采集线程合并消费需求，UI 与录制线程读取本地不可变缓存。空闲后首次请求会等待后台取得新快照，不能把没有主动请求造成的缓存变旧误判为相机失联。真正的缺帧、解码失败、反馈断流保留错误信息。

采样序号和时间属于来源帧，不随查询或复制更新。RC 输入租约和 policy 输出租约相互独立；读者必须明确释放，断连不是释放证明。异常退出时保留未确认资源与恢复记录，只有相应读者退出证明成立后才能回收。

## 验证

```bash
python3 scripts/test_scope.py
python3 scripts/test_scope.py --interface
python3 tests/example_docker_smoke.py --evidence-dir /tmp/connector-example
python3 tests/example_docker_smoke.py --ros --evidence-dir /tmp/connector-ros
python3 tests/manual/connector_acceptance.py --duration 30 --record --output /tmp/connector-cadence.json
```

Docker 验收使用隔离部署，`--ros` 使用独立 ROS 模拟器及随机 namespace/domain，并明确检查 RC 镜像无法导入 rclpy。可以用 `RC_TEST_RC_IMAGE`、`RC_TEST_ANCHOR_IMAGE` 指定测试镜像。实测报告区分配置频率、实际提交频率和回执延迟。Mock、Docker 和 ROS 模拟器不能替代真机验收。
