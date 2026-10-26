# 三路图像 LLM 离散动作策略

`llm_direction` 把同一轮的 `head`、`left_wrist`、`right_wrist` RGB 图像和 task 交给视觉 LLM，再生成普通 `(T,14)` 绝对 `epos` 动作块。它可以独立运行，也可以作为原生链成员。网络请求发生在 policy worker 内，机器人指令仍由 RC 控制线程发送。

## 执行规则

每只手选一个动作，两只手同时执行：

| 动作 | 含义 |
| --- | --- |
| `forward` / `backward` | 基座坐标系 +X / -X 平移 |
| `left` / `right` | 基座坐标系 +Y / -Y 平移 |
| `up` / `down` | 基座坐标系 +Z / -Z 平移 |
| `twist-clockwise` / `twist-counterclockwise` | 工具局部 X 轴正向 / 反向旋转 |
| `tilt-down-toward-table` / `tilt-up-away-from-table` | 工具局部 Y 轴正向 / 反向旋转 |
| `turn-left-across-table` / `turn-right-across-table` | 工具局部 Z 轴正向 / 反向旋转 |
| `open` / `close` | 仅改变对应夹爪目标 |
| `noop` | 保持该手基准位姿和夹爪值 |

这些名字、方向和旋转组合与 MCP 一致。LLM 不能指定距离、角度或任意机器人指令。默认每次移动 0.05 m 或旋转 0.1 rad，夹爪打开 -3.4、关闭 0；修改这些值应在 policy 配置中进行。

当输入剩余长度 **大于** `request_when_remaining` 时直接透传，不发送 API 请求；小于等于时同步请求 LLM。新动作从输入最后一个目标继续插值；输入为空则从 `state.eef` 开始。保留输入并追加 `chunk_size` 行，位置与夹爪线性插值，旋转走最短球面插值。左右手都为 `noop` 时也生成保持动作块，之后继续观察与决策。

默认 `sync`、`call_every_actions=1`、阈值 0、追加 30 行。只按照动作块剩余长度触发，**不等待实际到位反馈**。限速可能使一行目标需要多个控制周期才能发完，因此动作块长度并不承诺固定执行时长。框架在异步模式下仍按实际消费数量裁剪返回结果。

模型报告 `done=true` 后，请求现有 `runtime.pause()`。独立 policy 或整链最终成功返回后，RC 丢弃该轮输出、清空缓存并暂停。再次 `resume` 会重新观察与决策；`done` 不是永久完成锁。

## 配置 task、prompt 和供应商

编辑 `policy/llm_direction/policy.json` 的 `params`。出厂 `task` 为空，必须填写后才能加载该策略；基础 hold 启动不受影响。

| 字段 | 默认值 / 要求 |
| --- | --- |
| `task` | 必填任务文本 |
| `prompt` | 可编辑的决策提示词；程序另行附加动作和输出契约 |
| `request_when_remaining` | 0，非负且小于 `chunk_size` |
| `chunk_size` | 30，每次追加数量，1–4096 |
| `translation_m` / `rotation_rad` | 0.05 / 0.1，有限正数 |
| `gripper_open` / `gripper_close` | -3.4 / 0，有限数值 |
| `image_max_side` / `jpeg_quality` | 1024 / 85；保持比例、不放大小图 |
| `provider.timeout_s` | 30；SDK 网络超时，整轮还受 RC `timeouts.inference_s` 限制 |

模型不接收历史图像或历史对话。图像以 JPEG data URL 直接通过 HTTPS/HTTP 发给配置的供应商，三张分别携带相机名称；不会上传到另一个图床。编码总长度上限 16 MiB。共享内存视图只在回调期间借用，lease 要等回调释放确认或进程树退出证明才能回收。

供应商模板位于 `policy/llm_direction/providers/`：

| 文件 | API | 密钥变量 | 输出方式 |
| --- | --- | --- | --- |
| `deepseek.json` | DeepSeek Chat Completions，`deepseek-flash` | `DEEPSEEK_API_KEY` | `json_object` |
| `openai_compatible.json` | 自定义 Chat Completions，模型自行填写 | `LLM_API_KEY` | `json_schema`、`json_object` 或 `text` |
| `openai_decisions.json` | OpenAI Decisions，`gpt-6-luna` | `OPENAI_API_KEY` | `choice` |
| `openai.json` | OpenAI Responses，`gpt-6-luna` | `OPENAI_API_KEY` | `json_schema` |

模板是 `params.provider` 对象，不是独立 policy manifest。初始化不访问供应商，但会检查 task、配置及指定环境变量是否存在。自建无认证服务也需在对应变量提供其允许的占位凭证，因为官方 SDK 要求 API key 参数。

在启动 RC 前，可以从项目根目录运行以下示例，原子更新 task 和供应商对象：

```bash
python3 - <<'PY'
import json, os, tempfile
from pathlib import Path
path = Path('policy/llm_direction/policy.json')
config = json.loads(path.read_text())
config['params']['task'] = '把左侧的方块放入托盘，完成后停止'
config['params']['provider'] = json.loads(
    Path('policy/llm_direction/providers/deepseek.json').read_text())
fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.policy-', suffix='.json')
with os.fdopen(fd, 'w') as stream:
    json.dump(config, stream, ensure_ascii=False, indent=2)
    stream.write('\n')
os.replace(temporary, path)
PY
```

自定义供应商需要修改 `base_url`、`model` 与 `output_format`。`base_url` 为 API 前缀，不是完整 `/chat/completions` 地址；不得含密钥、用户名、查询参数或 fragment。无论是否支持服务端 JSON Schema，返回值都会在本地严格检查：只有 `left`、`right`、布尔 `done`，拒绝未知动作、重复键、额外字段和不完整输出。拒答、超时、格式错误、HTTP 错误会暂停；不会自动重试、修复 JSON、切换模型或切换接口。

Decisions 使用单题 226 个联合选项（左右手 15×15，再加完成），避免把有依赖的双手选择拆成独立问题。Responses 使用严格 Structured Outputs 并设置 `store=false`；其他接口遵循各供应商的数据保留规则。

官方参考：[DeepSeek Vision](https://api-docs.deepseek.com/guides/vision/)、[OpenAI Decisions](https://developers.openai.com/api/docs/guides/decisions)、[Decisions 接口](https://developers.openai.com/api/reference/resources/decisions/methods/create)、[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)。模型名称和账号可用范围由供应商决定。

## Docker 构建与启动

独立 Python 3.11 CPU 镜像不需要 GPU，不依赖 Qwen 的 CUDA 环境。Linux amd64 镜像可以在 x86 Ubuntu 22.04 上本机构建；在其他架构构建时可显式添加 `--platform linux/amd64`。

```bash
cp .env.llm.example .env.llm
# 用本地编辑器填写所选供应商的密钥；不要提交 .env.llm。
docker compose --env-file .env.llm -f compose.yaml -f compose.llm.yaml build policy-llm-direction
docker compose --env-file .env.llm -f compose.yaml -f compose.llm.yaml up -d
docker compose --env-file .env.llm -f compose.yaml -f compose.llm.yaml attach robot-control
```

首次启动整个项目可在 `up` 增加 `--build`，同时构建主框架与 hold。启动仍为 mock + hold，保持暂停。TUI 中输入：

```text
switch llm_direction --no-reset
show policy
resume
pause
```

使用 `--no-reset` 保留切换时的当前位置。切换成功仍保持暂停。终端 detach 使用 Ctrl-P、Ctrl-Q；`exit` 会关闭控制器。

ROS 模拟器组合：

```bash
docker compose --env-file .env.llm --profile ros-sim -f compose.yaml -f compose.llm.yaml -f compose.ros-sim.yaml up -d
```

真实 ACOne 的 Linux host-network 组合：

```bash
ROS_DOMAIN_ID=0 docker compose --env-file .env.llm -f compose.yaml -f compose.llm.yaml -f compose.acone.yaml up -d
```

之后的 attach、logs、stop、down 使用同一组文件和环境参数。机器人网络配置沿用 [Ubuntu 部署教程](ubuntu22-amd64-from-scratch.md)。自定义 API 在宿主机时可用 `http://host.docker.internal:端口/v1`，覆盖文件已提供 Linux host-gateway 映射；服务需要监听容器可访问的宿主地址。容器内的 `127.0.0.1` 指向容器自身。

此服务共享现有 IPC namespace 和 UDS 卷，不共享 PID namespace。健康检查只检查 host，不验证密钥、账户额度或模型权限。

## 配置保存、链与退出

独立运行时可以使用已有冷配置命令：

```text
pause
set policy.params.task "把物体向前移一次，然后停止"
set policy.params.prompt "根据三张图像选择小步动作，确定完成后停止。"
set policy.params.request_when_remaining 2
save-config policy
resume
```

每条 `set` 都执行一次冷重载，成功后保持暂停。更换供应商时一次设置完整的 `params.provider` JSON 对象，避免分步修改期间出现类型与输出格式不匹配。`api_key_env` 不得使用 PYTHON、CONDA、ROS 等保留变量名。密钥值始终只在环境变量中；`save-config` 只会保存变量名。

原生链通过 `manage-policy` 编排，所有成员须为 `epos`。默认 hold 和两个 Qwen 都是 `qpos`，不能直接加入同一链。`A → B → A` 复用 A 实例；若 A 生成的动作长度大于触发阈值，第二次 A 调用仅透传，不再请求 LLM。后续成员可以加工动作，但最终输出仍必须满足 epos 契约。

链模式不接受含糊的 `set policy.*`。可先独立切换到该成员修改并 `save-config policy`，然后重新应用链；或停止 RC、修改文件，再启动 RC。直接修改已被 registry 缓存的 manifest 不会自动热更新。

暂停、MCP 接管、配置冷重载和退出都会使旧动作失效。HTTP 请求可能仍需等返回或超时，迟到结果不能恢复动作。用户 `resume` 才允许下一次模型决策；硬超时或 worker 故障后先重新加载 policy。

```bash
docker compose --env-file .env.llm -f compose.yaml -f compose.llm.yaml stop
docker compose --env-file .env.llm -f compose.yaml -f compose.llm.yaml down
```

更新密钥需要重建容器环境；先正常 stop，再 `up -d --force-recreate`。更新依赖或适配包后先 build，再 up。正常停止先让 RC 结束回调与释放 policy，host 负责必要的进程树清理。失去释放证明的帧不能提前复用；全栈停止后 IPC namespace 最终销毁。输出、配置和快照绑定目录继续保留。

## 测试与验收

运行单元和本地 worker 测试需要额外测试环境安装 `requirements/llm-direction.lock`、本地 SDK、适配包及 `.[test]`，然后运行 `pytest tests/test_llm_direction.py tests/test_llm_providers.py tests/test_llm_worker.py`。主镜像不安装 OpenAI SDK，因此主环境会明确跳过需要该可选环境的测试。

真实跨容器测试使用本地 HTTP 替身，无真实密钥、无 API 费用：

```bash
docker build -f docker/rc.Dockerfile --target test -t robot-control-compose-tests:local .
docker build --platform linux/amd64 -f docker/llm-direction.Dockerfile -t robot-control-compose-llm-direction:local .
docker compose -p rc-llm-validation -f compose.yaml -f compose.llm.yaml -f compose.llm-test.yaml run --rm llm-integration
docker compose -p rc-llm-validation -f compose.yaml -f compose.llm.yaml -f compose.llm-test.yaml down -v
```

`compose.llm-test.yaml` 只用于隔离测试项目。它将供应商替换为容器内 HTTP 服务，并使用独立夹具卷。测试覆盖 Python 3.10 → amd64 Python 3.11、三图像素、RPC 无图像负载、四种 API、原生链、硬超时租约与迟到结果。

真实供应商权限、模型对实际场景的决策质量和实体机器人效果，需要使用部署者自己的账户与机器人单独验收；HTTP 替身与 ROS 模拟器不代表这部分已通过。软件验证结果另见 [LLM 验收记录](llm-direction-verification.md)。
