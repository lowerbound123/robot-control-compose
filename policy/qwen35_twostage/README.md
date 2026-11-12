# qwen35_twostage

本策略的业务实现位于 `src/rc_qwen35_twostage/`，模型源码、补丁与来源记录位于 `vendor/`，专用校验脚本位于 `scripts/`。这些文件均由本目录独立持有，不从其他 policy 或仓库级业务包导入。

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

若旧部署出现 `getpwuid(): uid not found: 1000`，检查对应 `compose.yaml` 的 `services.policy.environment` 是否有上述两项。修改 Compose 后重新 `policy switch qwen35_single` 或 `policy switch qwen35_twostage` 即会创建使用新环境的容器，无需重建镜像。仅在宿主 Shell export 或只写进 `deployment.env` 不会自动传给容器。

## 运行与模型切换

```bash
./scripts/compose.sh up -d --no-build
./scripts/compose.sh attach robot-control
```

```text
policy help qwen35_single
policy switch qwen35_single
resume
pause
policy qwen35_single switch-model model_alt.json
resume
policy switch qwen35_twostage
```

模型路径相对本策略目录；目标必须在现有 mounts 内可见。switch-model 的业务验证由 Qwen handler 完成，RC 只处理通用冷参数补丁和容器事务。旧模型进程与容器全部退出后才创建新容器；失败后停在空策略，不自动回滚或加载 hold。若 `params.task` 有覆盖值，切换模型配置后仍沿用该值；设为 `null` 才使用新模型配置的默认任务。

所有切换成功后暂停。默认 policy switch 保持当前位置；显式 `--reset` 才复位。链内两个 Qwen 都声明 switch-model，必须写目标，例如 `policy qwen35_twostage switch-model model_alt.json`。

```bash
./scripts/compose.sh down
```

查看动态容器可按 `.setup/runtime.json` 的 deployment_id 筛选 `io.robot-control.deployment` 标签。运行记录在 `outputs/runtime`，不要仅依靠基础 project 的 ps 列表判断是否全部退出。

## 任务文本热更新

两个策略均可使用`policy <实例> config set` 热更新 `params.task`，并保留注册命令 `switch-task <str>`。以单独运行 `qwen35_single` 为例，在 TUI 中执行：

```text
policy qwen35_single config set params.task "把杯子放到托盘里"
policy qwen35_single config show
policy qwen35_single config save
```

`params.task` 接受非空字符串，推理时映射为模型输入的 `task`。默认值为 `null`，表示使用初始化时从 `model.json` 读取的 `runtime.instruction`；`policy qwen35_single config set params.task null` 恢复该默认任务。链内同样支持 `null`，将前缀换为目标实例 ID 即可。空字符串、纯空白及其他类型会被拒绝。直接编辑 `model.json` 不会更新当前任务。

配置修改经 SDK 和 RC 两端确认后生效，保留容器、host、执行进程和已加载模型。configure 的 task 修改请求 reset，先等待旧推理退出、清空旧任务计划与历史，再使用新任务；不混合新旧任务的动作。task 配置修改完成后保持暂停；TUI 显式 resume，MCP 显式 return 才恢复。已有推理故障仍遵循原故障处理规则。注册的 switch-task 命令保留原 update_config effect 语义。

`switch-task` 的 handler 只验证非空文本并返回热补丁；`effect: update_config` 由 RC 提交到指定实例，确认后递增配置版本。单策略仍可使用 `policy qwen35_single config set params.task "任务文本"`。`policy qwen35_single config save` 将覆盖值写回当前策略的 `policy.yaml`，不改写 `model.json`。

`policy <实例> config set` 的前缀是运行实例 ID，沿用容器对应的实例命名：来源在链中只出现一次时直接用策略名；重复时按出现顺序用 `-0`、`-1` 等后缀。重复来源的裸策略名会报歧义，不会自动选择某个节点。例如：

```text
policy qwen35_single-0 config set params.task "抓取杯子"
policy qwen35_single-1 config set params.task "把杯子放到托盘里"
policy qwen35_twostage config set params.task "整理桌面"
policy qwen35_single-1 config set params.task null
policy qwen35_single-1 config save
```

配置入口由策略 configure 解析。task 请求 reset，model_config_path 请求原进程 reinitialize；旧 set policy.* 不接受。`switch-task` 也可按实例调用：

```text
policy qwen35_single switch-task "抓取杯子"
policy qwen35_twostage switch-task "把杯子放到托盘里"
policy qwen35_twostage config save
```

每条命令仅更新目标实例与逻辑链的配置版本，其他实例的配置、context 和模型保持不变。MCP 需先 `take`，再调用 `policy_command(instance_id="qwen35_twostage", command="switch-task", args=["新任务"], wait=True)`；完成后仍由 MCP 持有控制权并保持 policy 暂停。

已有本地 `policy.yaml` 不会被 setup 覆盖。升级时保留其他设置，在两个策略各自的清单中补充以下内容，并重新加载策略一次以启用新代码与白名单；后续任务更新均无需重载：

```yaml
params:
  model_config_path: model.json
  task: null
hot_reloadable:
  - params.task
tui_commands:
  switch-task:
    handler: switch_task
    args: [str]
    help: 热更新任务文本，保留容器和已加载模型
    effect: update_config
    timeout_s: 5
```

保留已有 `switch-model` 命令声明。注册热配置命令需要 RC 和 SDK 双端支持 `update_config` effect；旧端不支持时明确拒绝初始化，不会降级为普通命令或冷重载。模型、checkpoint、GPU、推理参数和 `params.model_config_path` 仍为冷配置。

## PyTorch 回退与 CUDA Graph

未安装可选 `flash-linear-attention` / `causal-conv1d` 时，模型使用随镜像安装的 PyTorch 回退实现。该实现的临时状态和输出张量必须直接在输入设备上分配；先在 CPU 创建零张量再 `.to(value)` 会在 CUDA Graph 录制时触发 `operation not permitted when stream is capturing`，随后产生 capture invalidated 错误。2026-10-27 的本地补丁修正了 chunk/recurrent 两条路径的三处分配，保持原有浮点计算和输出格式。

补丁文件直接只读挂载到 Transformers 5.2.0 的目标模块，加载模型前校验版本、目标文件和当前宿主源码哈希。修改 `policy/qwen35_twostage/vendor` 后重新 `policy switch qwen35_twostage` 或相应链即可，不需要 build；旧容器退出后新容器会重新绑定文件，包含原子替换编辑的文件。`policy/qwen35_twostage/scripts/check_patches.py` 只做校验，不复制、不备份、不写入 site-packages。`compile_model=false` 不会关闭优化后端自己创建的 CUDA Graph；跳过预热也会把相同错误延后到第一次推理。

可以先在目标 GPU 上运行不需要模型权重、相机或机器人的独立检查：

```bash
docker build --platform linux/amd64 \
  --build-context policy-deps=./policy/qwen35_twostage/dependencies \
  -f policy/qwen35_twostage/Dockerfile --target test \
  -t robot-control-compose-qwen35-graph-tests:local .
docker run --rm --gpus all --platform linux/amd64 \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -e USER=robot-control -e LOGNAME=robot-control -e HF_HUB_OFFLINE=1 \
  --workdir /workspace --mount "type=bind,source=$PWD,target=/workspace,readonly" \
  --mount "type=bind,source=$PWD/policy/qwen35_twostage/vendor/lerobot/policies/qwen3_5vla/transformers_replace/models/qwen3_5/modeling_qwen3_5.py,target=/usr/local/lib/python3.11/site-packages/transformers/models/qwen3_5/modeling_qwen3_5.py,readonly" \
  -e PYTHONPATH=/workspace/packages/policy-sdk/src:/workspace/policy/qwen35_twostage/src:/workspace/policy/qwen35_twostage/vendor \
  --entrypoint python robot-control-compose-qwen35-graph-tests:local \
  -c 'import torch,pytest; assert torch.cuda.is_available(), "CUDA unavailable"; raise SystemExit(pytest.main(["-q","-rs","-p","no:cacheprovider","tests/test_qwen35_cuda_graph.py"]))'
```

测试检查两条回退路径的设备分配、手算数值、实际图录制及修改输入后的重复回放。无 CUDA 的开发环境只执行设备/数值测试并明确跳过 GPU 用例；这不代表真实 CUDA Graph 验收。GPU 不支持 bfloat16 时会单独跳过该精度的用例，仍需选择与实际部署一致的精度验证。

CPU 替身、镜像构建、跨容器共享内存、GPU 推理和真机动作是不同验收层次，参见 [验证说明](../../docs/verification.md)。
