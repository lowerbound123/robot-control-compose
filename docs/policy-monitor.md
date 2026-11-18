# Policy 信息监控

RC 和 SDK 0.8 提供可选的 `monitor(context)`。打开面板后，RC 独立于控制频率，以 100 ms 目标周期采集一次；每个策略最多一个未完成读取。推理或 LLM 请求等待期间仍可刷新。所有面板默认关闭，关闭后停止采集。

## 编写回调

在 policy 的 `main.py` 添加同步函数，不需要修改 manifest：

```python
def monitor(context) -> dict[str, float | int | str]:
    return context.monitor()
```

类实现应返回独立、线程安全的快照。例如在 context 初始化时创建锁和标量字典，推理完成后短时间更新它们：

```python
import threading

class Context:
    def __init__(self):
        self._info_lock = threading.Lock()
        self._info = {"phase": "ready", "calls": 0, "latency_s": 0.0}

    def record_result(self, calls, latency_s):
        # 推理、HTTP 请求、图像编码在这个锁外完成。
        with self._info_lock:
            self._info = {
                "phase": "running",
                "calls": int(calls),
                "latency_s": float(latency_s),
            }

    def monitor(self):
        with self._info_lock:
            return dict(self._info)
```

这个类片段用于接入现有 context，不替代 `initialize`、`policy`、`reset_context`、`release`。回调必须快速、只读，不调用网络、模型或动作接口，不保存共享内存图像。它可以与推理、observe 和本地命令并行，所有访问同一组数据的写入方也必须遵守同一锁约定。

返回值约束：

- key 为 Python 内建 `str`；value 仅为内建 `float`、`int`、`str`。
- 不接受 bool、None、列表、字典、NumPy 标量、NaN 或 Infinity。将 NumPy 标量显式转成 `int(...)` 或 `float(...)`。
- 每次最多 128 项，紧凑 UTF-8 JSON 最大 64 KiB；超过限制整次读取失败，不截断内容。
- 每次结果按插入顺序完整替换上一份；删除的 key 会消失，空字典显示“暂无信息”。
- 没有回调的旧策略正常运行。异步函数、生成器或不接受 context 的函数不公布监控能力。

## π0.5 策略

加载策略后输入 `monitor pi05` 或 `monitor pi05_lift2` 打开面板，再输入一次即可关闭。链中的重复实例使用完整实例名，例如 `monitor pi05_lift2-0`。

| 想检查什么 | 看哪些字段 |
| --- | --- |
| 当前任务与模型 | `task`、`checkpoint`、`device`、`action_mode`、模型预测长度 `model_horizon` 和可执行长度 `chunk_size`。已加载配置提供时也显示 `model_dtype` 和 `num_inference_steps`。 |
| 模型请求的左右夹爪值 | `left_gripper_raw`、`right_gripper_raw` 按步显示最近一次成功预测返回的完整夹爪序列。取已换算为机器人单位的第 6、13 列（从 0 开始），保留阈值、增量、裁剪和融合前的值。等待下次预测、使用缓存或预测失败时保留上一组，初始化和 reset 后显示 `[]`。 |
| 下一批何时预测 | 剩余动作数 `cached_actions` 低于 `request_when_remaining` 时可以提交预测。`consumed_actions` 和 `control_epoch` 来自最近的策略回调，`last_trim` 表示最近一次收取结果时裁掉的过期步数。 |
| 后台预测卡在哪里 | `model_stage`、`model_stage_age_ms`、`inference_busy`。已提交且仍由调度器持有的请求显示 `pending=1`、`pending_age_ms`、`pending_remaining_ms`。预测成功后、回调收取结果前，可以看到 `inference_busy=0` 且 `pending=1`。 |
| 一次预测用了多久 | `load_ms`、`warmup_ms` 单独计时。首次成功预测后显示 `predict_ms`、`predict_avg_ms`、`predict_max_ms`。生产后端另报最近一次成功预测的预处理 `processor_ms`、模型前向 `model_forward_ms`、动作还原及夹爪后处理 `post_ms`。这些是宿主线程观察到的墙钟时间，GPU 操作可异步执行。 |
| 观测与故障 | 最近提交的三路 `*_image_shape` 按高×宽×通道显示。提交数 `inference_count`、成功预测数 `inference_completed`、后台预测异常数 `inference_errors`、裁剪累计数和复位次数按实例 context 累计。发生错误后显示 `last_error`，后台异常还会记录 `last_error_stage`。 |

预热单独统计。普通 reset 清除当前计划、待取结果和错误，保留累计计数和最近成功预测的耗时。监控仅读取标量快照，打开面板不会触发推理或额外的 CUDA 同步。

## OpenWAM MaaS 策略

在 TUI 输入 `monitor openwam_maas_acone` 或 `monitor openwam_maas_lift2` 打开该实例的面板。链中重复的实例使用完整名称，例如 `monitor openwam_maas_lift2-0`。再次输入同一命令即可关闭。

| 想检查什么 | 看哪些字段 |
| --- | --- |
| 连到哪个服务 | `service_origin` 显示所配置服务的协议、主机和端口，`protocol_version` 显示初始化握手已核对的版本。服务端的归一化组须在部署侧核对。 |
| 请求为什么还没回来 | `model_stage` 依次显示 `encode`（三路 PNG、Base64 和 JSON）、`send`、`wait_response`、`decode`（解码及校验）和 `post`（夹爪处理）。在途时结合 `model_stage_age_ms`、`pending_remaining_ms` 查看已等待时间和剩余期限。失败时查看 `last_error_stage` 和 `last_error`。 |
| 慢在客户端还是服务端 | 最近成功预测的分阶段耗时为 `encode_ms`、`send_ms`、`wait_response_ms`、`decode_ms`、`post_ms`。客户端总耗时看 `predict_ms`，平均和最大耗时看 `predict_avg_ms`、`predict_max_ms`。`server_latency_ms` 取自服务响应，缺少有效值时显示 `—`；客户端端到端耗时已包含服务处理时间。`last_request_bytes` 为该请求的 JSON 负载字节数。 |
| 何时发下一批 | `task`、三路 `*_image_shape`、`cached_actions`、`request_when_remaining`、`pending`、`inference_count` 和 `last_trim`。默认阈值 0，最后一步消费后才请求；正阈值包含等号。 |
| 夹爪规则改了哪些步 | `left_gripper_raw`、`right_gripper_raw` 是服务返回的 32 步绝对夹爪值。`left_gripper_processed`、`right_gripper_processed` 是同一批应用机型规则后的值，取自裁剪和轨迹融合之前。 |

等待下一次结果或请求失败时，面板保留上一批成功预测的夹爪和请求信息。reset 后夹爪显示 `[]`、请求字节数归零、服务耗时显示 `—`，实例累计计数保留。读取面板只访问当前实例的内存快照。

## 使用面板

监控命令使用当前运行实例的 ID。单例与来源目录同名；链中重复实例使用自动编号，例如 `a-0`、`a-1`：

```text
monitor my_policy
monitor my_policy
help
```

`monitor <实例>` 每次切换该面板；Tab 补全当前已加载且支持监控的策略。旧 monitor- 前缀和 on/off 参数不再接受。headless 和 MCP 不开启 TUI 面板，也不会持续打印监控数据。原有 `monitor` 仍显示机器人姿态。

日志占左侧约 2/3，监控占右侧约 1/3。每个实例一个框，按开启顺序排列，按内容换行和调整高度；右栏超高时单独滚动。字符串按普通文本显示，控制字符以转义文字显示。全部关闭后日志恢复全宽。

原生链 `a → b → a` 提供 `monitor a-0`、`monitor b`、`monitor a-1` 三个独立监控入口，分别读取各自 context；`monitor a` 不会指向其中某个实例。普通暂停和 MCP 接管时继续采集；重置、热配置和释放时先停止新读取，等待在途读取完成，再修改 context。同名实例重载保留面板并清空旧数据；切换或重新编号后不再加载的实例移除。若重载后删除了回调，面板显示不可用，可手动关闭。

## 错误与退出

回调异常或非法数据只影响面板，保留最后成功值并显示原因及更新时间；重复错误不逐帧写日志。一次读取超过 1 秒显示超时，继续等待原调用，不自动重试或追加线程。原调用正常返回后可以恢复；关闭再打开也不会绕过仍在执行的旧读取。

永久挂起的回调不能安全地强杀单个线程。重载或退出会按现有生命周期期限终止旧 worker；原生链所有独立实例沿用整组清理规则，强制清理仍返回非零状态。监控回包不证明任何图像租约已释放。

## 更新 Docker 部署

需要同步升级主端和 SDK 挂载源码，然后完整退出并重新创建基础容器及策略运行组。首次迁移到依赖镜像需要构建一次，之后普通源码变更无需 build。无需新增依赖、服务、GPU 或 API 密钥。默认 hold 部署：

## 镜像更新与验证

运行 `./setup.sh --policies <id>` 准备对应镜像，再通过 policy switch 创建新容器。monitor 只读活动实例快照，切换时会失效旧代次，不执行 Docker/socket I/O。动态生命周期验证见 [verification.md](verification.md)。
