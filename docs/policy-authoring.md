# 策略作者规范

SDK 0.11 的 `runtime.globals` 提供跨策略共享状态：直接读取共享内存，RC 统一提交写入，退出全量保存到 global.env。完整接口、并发与持久化约定见 [全局共享状态](global-state.md)。

可直接运行的完整本地范例见 [example 策略](../policy/example/README.md)：Python 3.14、动作透传、空输入保持、共享图片、同名命令、观测、监控、热配置与注释的 RC 暂停请求。

## 目录身份与声明

每个可执行策略实例独占一个 host 容器和受监督执行进程。来源目录名必须匹配 `[a-z0-9][a-z0-9_-]{0,47}`，manifest 的 id 可省略或与目录一致。运行实例另有身份，重复链成员自动编号；没有额外 host/socket 表。

```text
policy/example/
  policy.yaml
  main.py
  compose.yaml
```

```yaml
schema_version: 3
execution:
  mode: sync
  call_every_actions: 1
  control_mode: qpos
params:
  gain: 1.0
hot_reloadable:
- params.gain
```

```yaml
services:
  policy:
    image: your-prepared-policy-image:tag
```

image 只包含策略的第三方依赖并提供 `python` 命令；SDK 由管理器只读挂载。可添加 build、环境、GPU 和 bind mounts；部署配置归本目录。管理器注入唯一实例 host ID、socket、run/generation、用户身份、共享 rpc 卷、IPC anchor、只读 SDK 及本策略目录。host 的 `--source-policy-id` 固定来源目录，默认与 host ID 相同；实例 `a-0`、`a-1` 都从 `/workspace/policy/a` 加载，实现代码与相对资源路径保持原样。不要声明 sidecar、host PID、Docker socket 或 privileged。

相对 bind source 以策略目录为基准，由框架转换到宿主路径。Compose env 为本目录的 `deployment.env`。需要外部目录时写清宿主路径并提前创建。发现只读声明与文件是否存在，不 import main.py；新 Python 语法只由目标容器预检。

非 external 网络由管理器按运行实例项目重命名并独立清理，包括 Compose 自动生成及显式 `name` 的网络；其余网络选项保留。需要跨实例共用的已有网络应声明 `external: true`，其名称保持原配置，管理器不负责创建或清理。

策略有独立实现包时，在自己的 Compose 中声明来源。例如：

```yaml
    environment:
      PYTHONPATH: /workspace/packages/my-policy/src
      RC_SOURCE_MODULES: '{"my_policy":"/workspace/packages/my-policy/src"}'
    volumes:
      - type: bind
        source: ${RC_HOST_PROJECT_ROOT}/packages/my-policy/src
        target: /workspace/packages/my-policy/src
        read_only: true
        bind: {create_host_path: false}
```

`RC_SOURCE_MODULES` 是顶层 Python 模块名到源码根目录的 JSON 对象。管理器保留策略的 PYTHONPATH，统一加入 SDK；启动器在运行 host 前校验实际导入来源，错误则走现有失败收尾流程。主脚本和包修改后重新 switch 当前策略或整条链；依赖变更才重建镜像。不要在 entrypoint 中 pip install 或复制源码，详见 [部署准备](setup.md#依赖镜像与只读源码)。

## 回调

```python
from pathlib import Path
ASSET_ROOT = Path(__file__).resolve().parent

def initialize(config):
    return {"config": config}

def policy(context, state, action_chunk, images, config):
    return [state["qpos"]]

def reset_context(context):
    pass

def release(context):
    pass
```

四个同步函数必须在 main.py 顶层声明。辅助模块使用相对 import，资源路径以本策略目录或模型配置文件为基准，不依赖仓库 parents[2]。策略依赖只装在对应镜像中，RC 不安装 Torch 等模型框架。

可使用 SDK 的 `runtime`、`observe`、`monitor`、命令、外部资源接口。`runtime.pause(reason)` 只在当前推理回调线程使用，成功结果才携带暂停请求。运行时配置引用保持稳定，热补丁仅允许白名单叶字段，两端确认 config_version 后生效；RC 参数和身份/命令/时限属于冷配置。

## 任务完成与终止

SDK 0.10 新增 `runtime.finish(message="", *, outcome="completed")`。`outcome` 只接受 `completed`（任务完成）或 `terminated`（主动终止），`message` 为字符串。以下仅演示结果回传，实际策略应在自身判断满足结束条件时调用：

```python
def policy(context, state, action_chunk, images, config, *, runtime):
    runtime.finish("任务已完成", outcome="completed")
    return [state["qpos"]]  # 仍须返回符合本策略 control_mode 的合法动作
```

终止示例为 `runtime.finish("抓取未成功，需要外部处理", outcome="terminated")`。它只登记本轮结果，不抛出中断、不等待 MCP，也不直接改控制权。只能在当前 `policy()` 回调线程调用，不能用于 initialize、observe、monitor、命令处理或后台线程；旧 runtime 不得跨轮复用。

无论原生 chain 或 SDK `PolicyChain`，后续成员都继续运行，全部成功、动作合法且图像释放得到确认后才提交结果。无普通暂停时保留最早的 finish；任一 `runtime.pause` 优先于 finish。后续成员异常、超时、取消或非法结果不能被此前 finish 掩盖。RC 丢弃结束轮的全部动作、清空缓存并保持当前目标；不再发起新推理，正常模型保留。

有未结束的 `return_and_wait` 任务时，RC 将控制权交还 MCP，并返回 `result={"outcome":..., "message":..., "source":{"policy_id":...}}`，原生链来源还含从 1 开始的 `cell`。MCP 超时或断连不会撤销这个资格。没有等待任务时只暂停，所有权仍属于 policy，可显式 resume。普通 `runtime.pause` 会取消等待任务，回执 `reason="人工接管"`；原始 pause reason 仍记录在日志中。

RC 与 worker 通过 `runtime_tools` 协商 finish 能力。未协商时调用会明确报错，不会降级成 pause 或伪造完成。使用此 API 前同步更新 RC/SDK 源码并完整重建运行组；依赖变化时才重建镜像。SDK 仍仅依赖 NumPy 与 posix-ipc。

## 命令

```yaml
tui_commands:
  set-gain:
    handler: set_gain
    args:
    - float
    help: 设置任务参数
    timeout_s: 5
```

在 main.py 声明 handler，业务验证由策略完成。使用 `help example` 查看静态声明，即使尚未运行也可以查看；执行 `policy example set-gain 0.5` 需要该实例已运行。链内同名裸命令不再覆盖前一个成员，使用显式目标或 `<instance_id>-<command>`。重复实例分别注册命令，例如 `policy a-0 set-gain 0.5`、`a-1-set-gain 0.8`，原名 a 不会指向其中某个实例。策略命令不自动向 MCP 开放。

`effect=reload_policy` 的 handler 返回已有 `params.*` 冷叶字段补丁。例如 Qwen 的 `switch-model` 在旧实例验证模型配置，然后由 RC 执行完整清旧启新事务。链中只有目标实例的配置与版本变化，整组冷重建时其他实例保留当前配置。不得从 handler 调 Docker。部署参数通过本目录修改，在下次 switch 重新解析。

`save-config a-0` 或 `save-config policy a-0` 保存该实例的有效配置到来源目录 `policy/a/`，保留 YAML 注释和顺序，并将落盘 ID 写为 a。不会创建实例目录，也不会修改当前其他实例。重复实例存在时，`save-config a` 和 `save-config policy a` 都会报歧义且不写文件；必须明确选择一个实例。多个实例先后保存时，最后一次成功保存决定来源默认配置。`save-config policy` 在链模式仍保存策略顺序。

## 链、动作与租约

原生 chain：`{"schema_version":3,"kind":"chain","policies":["a","b","a"]}`。chain 自身无容器，每个环节独立实例化为 `a-0 → b → a-1`，分别初始化 context、执行进程和容器；回调收到的 `config.id` 也是实例名。只重复的策略编号，按该来源在链中的出现顺序从 0 开始；实例名最多 64 字符。`[a,a,a-0]` 产生实例名冲突，会在清理旧组前报错。链推理仍串行传递动作，pause/finish 来源、命令回执和外部 reader 都保留对应实例身份。

任何冷切换都销毁整个旧组，包括与新链相同的来源或实例。显式 switch 或重新应用链从来源默认配置创建新快照；实例命令引发的重载保留其他成员当前快照。链 YAML 始终保存来源名和顺序，不持久化每个环节的参数。SDK 的 `PolicyChain` 是另一个层次：在一个 worker 内组合模块；`runtime.globals` 继续是显式共享状态。

只有控制线程发送机器人动作和修改动作缓存。完整目标成功发送才消费；迟到推理、旧实例命令与旧版本回包不能越过切换屏障。策略失败不收回 MCP 已持有的控制权。

图像使用只读 POSIX fd/mmap，RPC 只传描述符。回调结束必须释放所有视图，禁止将数组或切片保存到 context、全局变量或后台线程；需要长期保留时 copy。租约占用的槽不会覆盖。共享内存尺寸与应用帧池容量都应匹配实际相机。

外部模型/HTTP 服务必须登记 owner、instance 与图像 reader，按协议明确释放。SDK worker 树退出证明、容器 CID/代次退出证明、外部 owner/instance 回执用途不同；本地容器删除不能替代外部释放。断连、socket 消失、CLI 超时均不是证明。

## 生命周期失败

默认策略和所有重载成功后保持暂停；显式 resume 才驱动推理。switch 默认不复位，--reset 才运动。release 超时由监督层和容器层兜底，完整证明允许后续初始化，但强制事实保留为非零退出。无法证明时进入 BLOCKED_CLEANUP，不启动新组、不回收未知 reader。

创建前保存意图，取消/超时后按本 generation 可信标签核对全部资源。所有清理均受同一绝对期限限制。容器 restart 固定 no；恢复必须通过统一生命周期入口，不自动复用旧 session 或回滚旧模型。
