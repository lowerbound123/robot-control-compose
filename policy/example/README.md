# example：本地策略能力范例

入口在 [main.py](main.py)，使用锁定的 Python 3.14 镜像，只依赖标准库和轻量 SDK（NumPy、posix-ipc）。代码按回调组织，附中文注释，适合复制后修改。

**非空 `action_chunk` 直接原样返回，不复制、不裁剪、不变换。** RC 第一次调用通常传入空动作，而 SDK 不接受空结果，所以空输入时返回一帧当前 `state["qpos"]`。这是独立运行的唯一动作例外。示例固定使用 `qpos`，初始化会拒绝 `epos/mixed`，避免将关节位置误作末端位姿；其他模式的动作格式见 [策略作者规范](../../docs/policy-authoring.md)。

透传表示原样保留上层动作，上层动作本身仍可驱动机器人。独立运行时的空输入分支跟随测得的当前姿态，不锁定初始化时的姿态。实际机器人动作仍由 RC 控制线程与限幅器处理。

## 准备与运行

在仓库根目录运行：

```bash
./setup.sh --policies example
./scripts/compose.sh up -d
./scripts/compose.sh attach robot-control
```

setup 从 `policy.json.example` 生成本机 `policy.json`，构建 `robot-control-compose-example:local`；不会启动策略。已有 RC 可以直接使用下面的命令。基础栈按现有 `startup_policy` 启动，example 不改变该设置。

```text
help example
switch example
example
policy example example
monitor-example on
resume
pause
monitor-example off
```

`switch example` 成功后保持暂停，需要显式 `resume`。无参数的 `example` 和 `policy example example` 返回同一类快照；前者是当前策略的裸命令，后者显式指定策略。它们不修改动作、缓存或控制权。`monitor-example on` 是 TUI 的本地监控命令，面板关闭时不持续采集；自定义命令不自动开放给 MCP。

退出后用项目入口清理动态策略和基础设施：

```text
exit
```

```bash
./scripts/compose.sh down
```

## 回调与数据

`main.py` 中提供了 `runtime.globals` 的注释示例：可将代理保存到 context，在推理、命令或 release 中共享 JSON 字段。默认示例不写入业务状态；启用方式、版本条件更新和退出保存规则见 [全局共享状态](../../docs/global-state.md)。

| 回调 | 演示内容 |
| --- | --- |
| `initialize(config)` | 验证配置、保留原始 config 引用、创建锁与统计 |
| `policy(..., runtime=...)` | 读取共享图片和 runtime 元数据；非空动作原样返回，空动作保持当前 qpos |
| `wants_observations(context)` | 初始化时声明订阅额外观测，本例返回 True |
| `observe(..., runtime=...)` | 与推理并发接收观测，只更新摘要，不返回动作 |
| `monitor(context)` | 快速、只读、独立的标量快照 |
| `example(context)` | manifest 注册的同名无参数命令，返回快照 |
| `validate_config(config)` | 初始化及热补丁应用前验证业务参数 |
| `reset_context(context)` | 清空计数和最近观测，保留配置引用与锁 |
| `release(context)` | 兼容 None、可重复调用的资源清理示例 |

`images[相机名]` 已由 SDK 映射为只读 NumPy 数组；`images.descriptors[相机名]` 提供时间戳等元数据。本例读取每张图的尺寸、dtype、只读标记、时间戳和第一个像素，将它们转换为普通数据，最终只在 context 保存 JSON 字符串。没有图像处理或整帧复制。

图像及切片只能在本次回调内使用，不能放入 context、全局变量、闭包或后台线程。确需长期保存时，必须在回调中显式 copy。SDK 在回调结束后关闭映射并确认释放，策略不自行 unlink 共享内存；断连不是释放证明。

推理、观测、命令和监控可能并发。所有共享统计遵守同一把锁；读图和日志输出在锁外完成。监控值只有内建 `str/int/float`，`images` 是摘要字符串；`released` 用 0/1 表示。`config_version`、`observation_id`、`consumed_actions`、`control_epoch` 表示最近一次 policy/observe 回调的信息，初始化或重置后为零/空值。暂停后仍能查询快照，但不会产生新的推理或观测。

`policy()` 中有一行默认注释的 RC 控制示例：

```python
# runtime.pause("example：演示暂停请求")
```

如取消注释，只能由当前 `policy()` 推理线程调用；它随成功结果提交给 RC，不直接发送机器人动作。仍须返回合法动作，链中后续成员也须成功。不能在 initialize、observe、命令或 monitor 中调用。本例不使用外部模型、网络服务、远程图片租约或命令触发冷重载。

## 热配置与日志

默认每 100 次 policy 调用打印一次 message；生命周期回调打印简短日志。观测和监控不会逐帧打印。

```text
set policy.params.message "新的示例消息"
set policy.params.log_every_n_calls 10
example
save-config policy
```

`message` 必须是字符串；`log_every_n_calls` 必须是正整数（bool 也不接受）。这两个叶字段列入 manifest 热更新白名单。SDK 保持 config 对象及其嵌套引用稳定，并在两端确认版本后生效，入口不复制配置。执行模式、控制模式和命令声明为冷配置。非法更新不会部分写入；如果补丁已发给 worker 后被 `validate_config` 拒绝，现有 SDK 会清理该会话，需要 `switch example` 重新加载。

链模式不使用含糊的 `set policy.*`：可先独立加载 example，修改并保存后再切回链。命令参数、帮助与超时都在 `tui_commands.example` 声明；本例的 `args: []` 表示无参数，扩展时可声明 `str/int/float/bool`，handler 对应接收 `context, *args`。

## 放在原生链中

创建本机 `policy/hold_example/policy.json`：

```json
{
  "schema_version": 3,
  "kind": "chain",
  "policies": ["hold", "example"]
}
```

准备成员镜像并在 RC 中切换：

```bash
./setup.sh --policies hold_example
```

```text
switch hold_example
policy example example
monitor-example on
resume
```

hold 先产生非空动作，example 原样返回它。两个可执行策略各占独立容器，链自身没有容器。也可使用链别名 `example-example` 查询命令。链中重复引用 example 会复用同一个实例和 context。

## 验证

在已安装项目测试依赖的环境中运行：

```bash
python3 -m pytest tests/test_example_policy.py tests/test_example_worker.py -q
```

真实 Docker 验收先准备 RC、anchor、hold 与 example 镜像，再运行独立脚本：

```bash
./setup.sh --policies hold,example
python3 tests/example_docker_smoke.py --evidence-dir /tmp/example-evidence
```

脚本在临时目录生成自己的配置，使用唯一部署身份，验证命令、共享图片、监控、热更新、原生链和退出清理，并将结果与日志写入指定证据目录。清理只针对本次测试资源。完整回归方法见 [验证文档](../../docs/verification.md)。软件测试使用 mock 和独立 Docker 部署；这些结果不替代真实机器人验收。
