# Qwen3.5 策略

`qwen35_single` 与 `qwen35_twostage` 各自独占容器、host instance、模型和 context，共享同一个 `robot-control-compose-qwen35:local` Python 3.11 镜像。依赖版本和哈希在各目录 `dependencies/requirements.lock`；Dockerfile 和 build 声明各自完整。单独准备 twostage 不依赖先安装 single。

## 准备

```bash
./setup.sh --config-only
# 编辑各策略 deployment.env 和 model.json，准备所需目录与权重
./setup.sh --policies qwen35_single,qwen35_twostage
```

各目录有：

- `policy.json`：协议、execution、params、命令；model_config_path 默认 `model.json`。
- `model.json`：模型、workflow、推理与动作配置。
- `deployment.env`：GPU_DEVICE、MODELS_DIR、HF_CACHE_DIR、HF_HUB_OFFLINE。
- `compose.yaml`、`Dockerfile`、`dependencies/requirements.lock`：部署和构建。

模型和缓存路径相对策略目录，或使用宿主绝对路径。两个策略可以共用同一个外部模型/cache 目录。更换 GPU 或 mount 后重新 switch，配置会重新解析；运行切换不构建、安装或下载权重。NVIDIA 驱动和 Container Toolkit 需在目标机配置，setup 构建成功不代表 GPU 已可用。

两个 Qwen 的 Compose 环境同时设置 `USER=robot-control` 和 `LOGNAME=robot-control`，供 Python/PyTorch 查询用户名、创建临时缓存。实际进程仍使用 setup 记录的宿主 UID/GID；这两个变量不会创建系统账号或改变权限。PyTorch 2.10 在导入 torchvision 时就可能查询用户名，即使 `compile_model=false` 也需要这些变量。

若旧部署出现 `getpwuid(): uid not found: 1000`，检查对应 `compose.yaml` 的 `services.policy.environment` 是否有上述两项。修改 Compose 后重新 `switch qwen35_single` 或 `switch qwen35_twostage` 即会创建使用新环境的容器，无需重建镜像。仅在宿主 Shell export 或只写进 `deployment.env` 不会自动传给容器。

## 运行与模型切换

```bash
./scripts/compose.sh up -d --no-build
./scripts/compose.sh attach robot-control
```

```text
help qwen35_single
switch qwen35_single
resume
pause
policy qwen35_single switch-model model_alt.json
resume
switch qwen35_twostage
```

模型路径相对本策略目录；目标必须在现有 mounts 内可见。switch-model 的业务验证由 Qwen handler 完成，RC 只处理通用冷参数补丁和容器事务。旧模型进程与容器全部退出后才创建新容器；失败后停在空策略，不自动回滚或加载 hold。

所有切换成功后暂停。默认 switch 保持当前位置；显式 `--reset` 才复位。链内两个 Qwen 都声明 switch-model，必须写目标，例如 `policy qwen35_twostage switch-model model_alt.json`。

```bash
./scripts/compose.sh down
```

查看动态容器可按 `.setup/runtime.json` 的 deployment_id 筛选 `io.robot-control.deployment` 标签。运行记录在 `outputs/runtime`，不要仅依靠基础 project 的 ps 列表判断是否全部退出。

CPU 替身、镜像构建、跨容器共享内存、GPU 推理和真机动作是不同验收层次，参见 [验证说明](verification.md)。
