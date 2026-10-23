# Qwen3.5 Docker 部署与使用

`qwen35_single` 和 `qwen35_twostage` 复用同一个 `robot-control-compose-qwen35:local` 镜像，分别运行在两个容器内。两边的模型、context、动作缓存、推理线程和监督进程独立。镜像只构建一次；共用 GPU 时，两个已加载模型仍会分别占用显存。

| 服务 | policy ID | host / UDS |
| --- | --- | --- |
| `policy-qwen35-single` | `qwen35_single` | `qwen35-single` / `/run/robot-control/qwen35-single.sock` |
| `policy-qwen35-twostage` | `qwen35_twostage` | `qwen35-twostage` / `/run/robot-control/qwen35-twostage.sock` |

环境为 Python 3.11、Torch 2.10.0 + CUDA 12.8、torchvision 0.25.0、Transformers 5.2.0、NumPy 2.2.6、posix-ipc 1.3.2。完整版本及包哈希见 `requirements/qwen35.lock`，基础镜像 digest 固定在 `docker/qwen35.Dockerfile`。所有程序依赖和 Transformers 补丁在构建阶段安装，不使用 Conda。主容器继续使用 Python 3.10，hold 继续使用 Python 3.14。

## 1. 准备 Ubuntu 22.04 与 NVIDIA Docker

先完成 [从零构建教程](ubuntu22-amd64-from-scratch.md) 中的 Docker/Compose 安装。生产目标为 x86_64 Ubuntu 22.04 + NVIDIA GPU；此覆盖配置明确选择 `linux/amd64`。

宿主机需要能够正常运行 `nvidia-smi` 的 NVIDIA 驱动，以及 NVIDIA Container Toolkit。使用支持 CUDA 12.8 和实际 GPU 型号的驱动；部署建议使用 570.124.06 或更高的兼容驱动，不以 `nvidia-smi` 的 CUDA 数字代替容器内 Torch 检查。驱动选型见 [CUDA 12.8.1 发布说明](https://docs.nvidia.com/cuda/archive/12.8.1/cuda-toolkit-release-notes/index.html)。CUDA 用户态依赖随 pip wheels 安装在镜像内，宿主无需创建 Python 环境。

按 [NVIDIA 官方安装说明](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) 安装 Toolkit。Ubuntu 的安装与 Docker 配置命令如下；Docker 重启会影响这台电脑上已有容器，应在控制栈停止时操作：

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends ca-certificates curl gnupg2
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
  sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
nvidia-smi
```

## 2. 配置 GPU、模型与缓存

在项目根目录创建 `models`、`cache/huggingface`。将 `.env.qwen.example` 中的变量合并到已有 `.env`；保留已有 ROS、MCP 等配置：

```dotenv
QWEN_SINGLE_GPU=0
QWEN_TWOSTAGE_GPU=0
QWEN_MODELS_DIR=./models
QWEN_HF_CACHE_DIR=./cache/huggingface
QWEN_HF_HUB_OFFLINE=0
```

两个 GPU 变量分别选择宿主设备，支持设备索引或 GPU UUID。默认都选 GPU 0；双卡可以将 `QWEN_TWOSTAGE_GPU` 改为 `1`。每个容器只暴露指定 GPU，策略内部仍使用可见设备 `cuda:0`。Compose 使用 NVIDIA device reservation，见 [Docker GPU 配置说明](https://docs.docker.com/compose/how-tos/gpu-support/)。

默认模型目录布局：

```text
models/
  qwen35/
    single/pretrained_model/
      config.json
      stats.json
      model.safetensors
    two_stage/pretrained_model/
      config.json
      stats.json
      model.safetensors
  configs/
    experiment-single.json
cache/huggingface/
```

宿主 `QWEN_MODELS_DIR` 只读挂载到容器 `/models`。模型配置中的路径必须是**容器路径**，不能直接填写未挂载的宿主路径。两个策略的默认 `model.json` 已指向上述布局；任务指令、统计 key、schema、动作表示和状态偏移必须按现场 checkpoint 核对。

每个 checkpoint 要求类型 `qwen3_5vla_ki_wan`、chunk size 50、完整 `stats.json` 和单文件 `model.safetensors`；本适配器不接受仅 bin 或分片索引的权重。`stats_key` 对应的 state/action 统计必须合并为有限的 14 维数组，schema 必须支持三相机双臂。本项目不分发权重。

checkpoint 的 `vlm_model_name_or_path` 指向的基础模型也必须可见。现有 processor 默认使用 `Qwen/Qwen3.5-2B`；将对应基础 VLM、processor/tokenizer 资源准备到共享 HF 缓存。若 checkpoint 中记录了另一台机器的绝对路径，将模型挂载到对应容器路径或修正 checkpoint 配置。WAN 视频模型不参与当前 optimized action-only 推理。

默认允许 Hugging Face 缓存未命中时下载；全部资源准备完成后可设置 `QWEN_HF_HUB_OFFLINE=1` 并重新创建 Qwen 容器，做离线验收。两个容器共享缓存目录，模型权重与缓存不会进入 Git 或 Docker build context。

## 3. 构建一个 Qwen 镜像

以下命令均在项目根目录执行：

```bash
mkdir -p models cache/huggingface
docker compose -f compose.yaml -f compose.qwen.yaml build \
  robot-control policy-hold policy-qwen35-single
```

只有 `policy-qwen35-single` 定义 Qwen build；另一个服务直接引用相同的镜像标签。首次构建需要下载数 GB 的 Torch/CUDA 依赖，后续构建会复用缓存。无需分别构建两个模型环境。

检查镜像和 GPU：

```bash
docker image inspect robot-control-compose-qwen35:local --format '{{.Os}}/{{.Architecture}} {{.Id}}'
docker run --rm --platform linux/amd64 -i --entrypoint python \
  robot-control-compose-qwen35:local - < tests/qwen_image_probe.py
docker compose -f compose.yaml -f compose.qwen.yaml run --rm \
  --entrypoint python policy-qwen35-single -c \
  'import sys,numpy,torch; print(sys.version); print(numpy.__version__,torch.__version__,torch.version.cuda); assert torch.cuda.is_available(); print(torch.cuda.get_device_name(0))'
docker compose -f compose.yaml -f compose.qwen.yaml run --rm \
  --entrypoint python policy-qwen35-twostage -m pip check
docker compose -f compose.yaml -f compose.qwen.yaml run --rm \
  --entrypoint python policy-qwen35-single /opt/qwen35/scripts/install_qwen35_patches.py --check
```

如需预先下载默认基础模型到共享缓存，可在已构建的容器内执行（不会启动控制器或加载策略）：

```bash
docker compose -f compose.yaml -f compose.qwen.yaml run --rm \
  --entrypoint hf policy-qwen35-single download Qwen/Qwen3.5-2B
```

## 4. 启动与切换

选择一种部署组合，之后 attach、logs、stop、down 都沿用相同的文件组合：

```bash
# Mock 机器人 + 两个 Qwen host
docker compose -f compose.yaml -f compose.qwen.yaml up -d --no-build
docker compose -f compose.yaml -f compose.qwen.yaml attach robot-control

# 独立 ROS 模拟器；务必启用 ros-sim profile
docker compose --profile ros-sim -f compose.yaml -f compose.qwen.yaml -f compose.ros-sim.yaml up -d --no-build
docker compose --profile ros-sim -f compose.yaml -f compose.qwen.yaml -f compose.ros-sim.yaml attach robot-control

# 真机 ACOne，先核对 ROS_DOMAIN_ID、话题与安全参数
ROS_DOMAIN_ID=0 docker compose -f compose.yaml -f compose.qwen.yaml -f compose.acone.yaml up -d --no-build
ROS_DOMAIN_ID=0 docker compose -f compose.yaml -f compose.qwen.yaml -f compose.acone.yaml attach robot-control
```

不要同时运行这些组合。启用 Qwen 覆盖配置后，RC 会等待两个 host 的 RPC 健康检查通过；健康检查不会加载模型，不能替代 checkpoint 验证。默认启动仍为 hold，处于暂停状态。

TUI 中按需选择策略：

```text
switch qwen35_single --no-reset
resume
pause
switch qwen35_twostage --no-reset
resume
```

`--no-reset` 保持当前机器人位置。初始化成功意味着模型已加载、完成预热，随后仍保持暂停，必须显式 `resume`。只启动默认 `docker compose up -d` 不会启动两个 Qwen host；此时选择 Qwen 会提示 host 不可连接。

共用镜像不会额外卸载另一策略。实际加载哪些实例、切换时关闭哪些运行组、原生链重复成员如何复用，仍由框架现有生命周期决定。本轮保持原 Qwen 的单生成器缓存调度约束；直接把两个 Qwen 串成 `single → twostage` 会因后级收到不属于自己缓存的动作而被拒绝，不新增双模型融合规则。

## 5. 更换 checkpoint 与运行行为

在相应 Qwen 激活时执行：

```text
switch-model /models/configs/experiment-single.json
resume
save-config policy
```

参数指向完整模型 JSON，不是权重目录。可复制该策略的 `model.json` 作为模板；single 固定接受 `workflow: "single"`，two-stage 固定接受 `"two_stage"`。相对模型配置路径基于项目根 `/workspace`，JSON 中相对 checkpoint 路径基于 JSON 所在目录。

验证失败仅使这次命令失败，旧模型继续可用；验证成功则按原有冷重载流程暂停、废弃旧动作、关闭旧运行组并确认退出，再加载新模型。同路径也强制重载。成功回执到达后仍暂停；加载失败保持故障暂停，不自动回滚。`save-config policy` 才持久化新的配置路径。MCP 接管期间拒绝这类重载命令。

调度保持原实现：首次预测 50 步，剩余动作严格少于 30 且没有在途任务时后台补充。模型加载、预热、推理和释放在该实例的推理线程串行执行。后台持有三路图像及 qpos 的副本；回调返回后关闭共享内存只读映射，再确认 `images_released`。

结果按实际消费的完整目标数裁剪，在 canonical absolute 空间做原有融合，再应用一次夹爪处理。two-stage 默认每 3 次预测刷新 subtask，最多生成 128 个 token。暂停后 resume/reset 清空旧上下文，保留当前实例的权重；切换和冷重载遵循框架现有释放机制。

初始化、单次回调、释放默认超时为 600/60/30 秒；模型后台推理默认 60 秒，从提交时计时。调整模型超时须同步核对 RC timeout，调整释放超时须同步增加 Compose 停止宽限。任何致命故障仍需手动重载并 resume；断连本身不构成图像读者退出证明。

## 6. 停止、更新与排查

先在 TUI 输入 `pause`、`exit`，然后使用原来的文件组合停止整个栈，例如：

```bash
docker compose -f compose.yaml -f compose.qwen.yaml down
docker compose -f compose.yaml -f compose.qwen.yaml build policy-qwen35-single
docker compose -f compose.yaml -f compose.qwen.yaml up -d --no-build
```

更新共享依赖需要重新构建一次 Qwen 镜像，并重新创建两个 Qwen 容器。只 `restart` 不会换镜像或更新环境变量。更新挂载的策略入口或模型配置后，要显式重载策略。不要 `docker exec` 启动第二个控制器。

```bash
docker compose -f compose.yaml -f compose.qwen.yaml ps -a
docker compose -f compose.yaml -f compose.qwen.yaml logs --tail=100 policy-qwen35-single policy-qwen35-twostage
```

| 现象 | 检查 |
| --- | --- |
| `could not select device driver` | NVIDIA Container Toolkit 是否安装且配置到当前 Docker daemon |
| host healthy，但模型加载失败 | checkpoint 目录、统计、模型类型、HF 缓存、GPU 和显存；healthy 只验证 RPC |
| CUDA unavailable | 容器 GPU reservation、宿主驱动、目标 GPU 的兼容性；执行第 3 节实际 Torch 检查 |
| 显存不足 | 两个容器即使同镜像也独占各自模型内存；可分别选择 GPU，或只在需要时加载对应策略 |
| checkpoint/基础模型路径不存在 | 容器内 `/models` 挂载与 checkpoint 的基础模型路径；不要使用未挂载的宿主绝对路径 |
| 无法证明旧读者退出 | 停止对应完整 Compose 栈，重建 IPC 命名空间后再启动 |

## 7. 自动测试与硬件验收

不需要 GPU 的模型数值测试使用同一 CUDA 依赖镜像，但只替换最重的模型调用：

```bash
docker build --platform linux/amd64 -f docker/qwen35.Dockerfile --target test \
  -t robot-control-compose-qwen35-tests:local .
docker run --rm --platform linux/amd64 robot-control-compose-qwen35-tests:local
```

真实跨容器测试使用隔离项目，取消 GPU reservation，仅在测试入口替换模型 backend；保留真实 Python 3.11、Qwen scheduler、UDS、POSIX 图像和监督进程。不要将测试覆盖配置用于机器人部署：

```bash
docker compose -p rc-qwen35-validation -f compose.yaml -f compose.qwen.yaml -f compose.qwen-test.yaml build qwen-fixtures
docker compose -p rc-qwen35-validation -f compose.yaml -f compose.qwen.yaml -f compose.qwen-test.yaml run --rm qwen-integration
docker compose -p rc-qwen35-validation -f compose.yaml -f compose.qwen.yaml -f compose.qwen-test.yaml down -v
# 另起临时项目，在暂停链中加载两个模型替身，再检查 Compose stop/release/退出码
python3 tests/qwen_compose_stop.py
```

真实 GPU 验收需在目标机器上分别加载两个 checkpoint，检查首次 50 步、持续补充、two-stage subtask 刷新、同路径重载、双容器同时加载的显存占用、退出后的资源回收。进一步通过 ROS 模拟器验证后再开展真实机器人验收。CPU 替身与跨容器测试不代替真实模型、CUDA Graph 或机器人验收；结果见 [验收记录](verification.md)。
