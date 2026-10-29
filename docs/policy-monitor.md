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

本轮没有给 hold、Qwen 或 llm_direction 增加默认业务指标；需要在对应入口中添加这个可选回调后才能打开其面板。

## 使用面板

`policy_id` 是 manifest 的 `id`，不是目录名或容器名：

```text
monitor-my_policy
monitor-my_policy on
monitor-my_policy off
help monitor-my_policy
```

无参数表示切换；Tab 补全当前已加载且支持监控的策略。`monitor-` 是保留命令前缀，不能用在自定义 `tui_commands` 或原生链生成的命令别名中。headless 和 MCP 不开启 TUI 面板，也不会持续打印监控数据。原有 `monitor` 仍显示机器人姿态。

日志占左侧约 2/3，监控占右侧约 1/3。每个唯一策略一个框，按开启顺序排列，按内容换行和调整高度；右栏超高时单独滚动。字符串按普通文本显示，控制字符以转义文字显示。全部关闭后日志恢复全宽。

原生链 `A → B → A` 只显示一个 A 和一个 B。普通暂停和 MCP 接管时继续采集；重置、热配置和释放时先停止新读取，等待在途读取完成，再修改 context。同名重载保留面板并清空旧数据；切换后不再加载的成员移除。若重载后删除了回调，面板显示不可用，可手动关闭。

## 错误与退出

回调异常或非法数据只影响面板，保留最后成功值并显示原因及更新时间；重复错误不逐帧写日志。一次读取超过 1 秒显示超时，继续等待原调用，不自动重试或追加线程。原调用正常返回后可以恢复；关闭再打开也不会绕过仍在执行的旧读取。

永久挂起的回调不能安全地强杀单个线程。重载或退出会按现有生命周期期限终止旧 worker；Compose 同一 host 内共享执行进程的策略沿用整组清理规则，强制清理仍返回非零状态。监控回包不证明任何图像租约已释放。

## 更新 Docker 部署

需要同步升级主端和 SDK 挂载源码，然后完整退出并重新创建基础容器及策略运行组。首次迁移到依赖镜像需要构建一次，之后普通源码变更无需 build。无需新增依赖、服务、GPU 或 API 密钥。默认 hold 部署：

## 镜像更新与验证

运行 `./setup.sh --policies <id>` 准备对应镜像，再通过 switch 创建新容器。monitor 只读活动实例快照，切换时会失效旧代次，不执行 Docker/socket I/O。动态生命周期验证见 [verification.md](verification.md)。
