# Qwen3.5 策略

`qwen35_single` 与 `qwen35_twostage` 各自独占容器、host instance、模型和 context，共享同一个 `robot-control-compose-qwen35:local` Python 3.11 镜像。依赖版本和哈希在各目录 `dependencies/requirements.lock`；Dockerfile 和 build 声明各自完整。单独准备 twostage 不依赖先安装 single。

## 准备

```bash
./setup.sh --config-only
# 编辑各策略 deployment.env 和 model.json，准备所需目录与权重
./setup.sh --policies qwen35_single,qwen35_twostage
```

各目录有：

- `policy.yaml`：协议、execution、params、命令；model_config_path 默认 `model.json`。
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

## PyTorch 回退与 CUDA Graph

未安装可选 `flash-linear-attention` / `causal-conv1d` 时，模型使用随镜像安装的 PyTorch 回退实现。该实现的临时状态和输出张量必须直接在输入设备上分配；先在 CPU 创建零张量再 `.to(value)` 会在 CUDA Graph 录制时触发 `operation not permitted when stream is capturing`，随后产生 capture invalidated 错误。2026-10-27 的本地补丁修正了 chunk/recurrent 两条路径的三处分配，保持原有浮点计算和输出格式。

补丁文件直接只读挂载到 Transformers 5.2.0 的目标模块，加载模型前校验版本、目标文件和当前宿主源码哈希。修改 `vendor/qwen35` 后重新 `switch qwen35_single` 或相应链即可，不需要 build；旧容器退出后新容器会重新绑定文件，包含原子替换编辑的文件。`scripts/install_qwen35_patches.py` 保留历史名称，但现在只做校验，不复制、不备份、不写入 site-packages。`compile_model=false` 不会关闭优化后端自己创建的 CUDA Graph；跳过预热也会把相同错误延后到第一次推理。

可以先在目标 GPU 上运行不需要模型权重、相机或机器人的独立检查：

```bash
docker build --platform linux/amd64 \
  --build-context policy-deps=./policy/qwen35_single/dependencies \
  -f policy/qwen35_single/Dockerfile --target test \
  -t robot-control-compose-qwen35-graph-tests:local .
docker run --rm --gpus all --platform linux/amd64 \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -e USER=robot-control -e LOGNAME=robot-control -e HF_HUB_OFFLINE=1 \
  --workdir /workspace --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  --mount "type=bind,source=$PWD/vendor/qwen35/lerobot/policies/qwen3_5vla/transformers_replace/models/qwen3_5/modeling_qwen3_5.py,target=/usr/local/lib/python3.11/site-packages/transformers/models/qwen3_5/modeling_qwen3_5.py,readonly" \
  -e PYTHONPATH=/workspace/packages/policy-sdk/src:/workspace/packages/qwen35-policy/src:/workspace/vendor/qwen35 \
  --entrypoint python robot-control-compose-qwen35-graph-tests:local \
  -c 'import torch,pytest; assert torch.cuda.is_available(), "CUDA unavailable"; raise SystemExit(pytest.main(["-q","-rs","-p","no:cacheprovider","tests/test_qwen35_cuda_graph.py"]))'
```

测试检查两条回退路径的设备分配、手算数值、实际图录制及修改输入后的重复回放。无 CUDA 的开发环境只执行设备/数值测试并明确跳过 GPU 用例；这不代表真实 CUDA Graph 验收。GPU 不支持 bfloat16 时会单独跳过该精度的用例，仍需选择与实际部署一致的精度验证。

CPU 替身、镜像构建、跨容器共享内存、GPU 推理和真机动作是不同验收层次，参见 [验证说明](verification.md)。
