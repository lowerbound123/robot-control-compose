# 策略作者规范

SDK 0.14 增加公共热字段 `execution.on_backend_switch`，默认 `reinitialize_policy`，可选 `reset`。所有策略从初始化时建立可重绑定的context身份，不再需要声明重初始化命令才能参与backend切换。reset必须结束旧后台请求、清空旧机器人状态；release/initialize沿用旧绑定释放、新绑定初始化的语义。详见 [backend 热切换](backend-switching.md)。

SDK 0.11 的 `runtime.globals` 提供跨策略共享状态：直接读取共享内存，RC 统一提交写入，退出全量保存到 global.env。完整接口、并发与持久化约定见 [全局共享状态](global-state.md)。

可直接运行的参考实现见 [example 策略](../policy/example/README.md)：Python 3.14、三种控制模式、动作透传、共享图片、观测/监控、热配置、实例冷重载、共享状态、pause/finish 及可选外部 reader。新增或修改公共能力时应一起更新 example 和真实 worker 的验收路径。

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

`RC_SOURCE_MODULES` 是顶层 Python 模块名到源码根目录的 JSON 对象。管理器保留策略的 PYTHONPATH，统一加入 SDK；启动器在运行 host 前校验实际导入来源，错误则走现有失败收尾流程。主脚本和包修改后重新 policy switch 当前策略或整条链；依赖变更才重建镜像。不要在 entrypoint 中 pip install 或复制源码，详见 [部署准备](setup.md#依赖镜像与只读源码)。

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

配置入口是 `policy <实例ID> config show|set|save`，例如 `policy example-1 config set params.message 新消息`。目标必须是明确运行实例；歧义来源和未运行实例都拒绝。`set` 把字段名及其后完整原文交给策略的 `configure(context, candidate, key, value, callback) -> None`，RC 不解析策略值。策略校验并修改 candidate 中的 `params` / `execution`，不能修改实例 ID、命令声明等结构。没有 configure 的策略仍可运行，但配置编辑明确失败。

configure 可调用 `callback("reset")` 或 `callback("reinitialize")`：前者在提交新配置时重置原 context，后者在原执行进程和已加载模块中 release/initialize；两者完成后保持暂停，需显式恢复。不调用则普通提交。回调只登记意图，不能立即执行生命周期；重复同一种请求幂等，冲突请求拒绝，函数退出后 callback 失效。函数只能返回 None。校验、初始化和提交均复核目标、配置版本及控制代次；初始化或提交不确定时不自动回滚或重试。

普通提交保持原 config 对象引用，双端确认后更新目标及逻辑链版本；其他成员的配置与版本不变。重初始化暂停整链并等待读者确认。保存使用 `policy <实例ID> config save`，只落盘已提交的有效配置。旧 `set policy.*`、`set <实例>.*`、裸命令及实例前缀别名均不接受。RC 冷草稿、全局状态和姿态命令见 [统一命令树](commands.md)。

```yaml
tui_commands:
  set-gain:
    handler: set_gain
    args:
    - float
    help: 设置任务参数
    timeout_s: 5
```

在 main.py 声明 handler，业务验证由策略完成。使用 `policy help example` 查看静态声明，即使尚未运行也可以查看；执行 `policy example set-gain 0.5` 需要该实例已运行。所有策略命令都必须写出 `policy <实例> <命令>`。重复实例分别注册命令，例如 `policy a-0 set-gain 0.5`、`policy a-1 set-gain 0.8`，原名 a 不会指向其中某个实例。声明的命令同时向 MCP 开放：`policy_commands(policy_id=...)` 和 `help(policy_id=..., command=...)` 只读查询参数、时限、effect 与运行状态，不导入模型；重复来源查询展开全部实例。

MCP 执行必须先 `take`，再调用 `policy_command(instance_id=..., command=..., args=[...])`；参数保留 JSON 原生 str/int/float/bool 类型，布尔值不能作为整数，字符串不自动转为数字。`command("policy <实例> <命令> ...")` 受支持，沿用本地字符串参数解析。工具 `timeout_s` 仅限制回执等待，handler 执行时限仍来自 manifest。等待超时或断连不取消已受理命令。普通命令不自动恢复推理，三种生命周期 effect 完成后继续由 MCP 持有控制权，显式 `return` 或 `return_and_wait` 才恢复 policy。

`effect: update_config` 提交热配置补丁。handler 只验证并返回已有 `params.*` 白名单叶字段的非空补丁，不直接修改 context 或配置。RC 使用现有 SDK `update_config` 事务，在双端确认后更新目标配置及逻辑链版本；配置对象、context、模型、进程和容器保持不变。TUI 调用保留原运行/暂停状态，MCP 调用仍需先 take，完成后保持 MCP 控制权和 policy 暂停。链中必须解析为明确实例，其他成员的配置与版本不变；`policy example-1 set-message "新消息"` 展示该用法。命令回包与 ACK 提交都复核 worker、配置版本、控制权及动作代次；未确认或失效的提交沿用热更新故障清理，不自动重试。该 effect 通过 `tui_command_effects_v2` 双端协商，需同步更新 RC/SDK。

`effect: reload_policy` 只重建命令目标实例的容器、host 和执行进程。`effect: reload_all_policy` 重建整个活动运行组。两者的 handler 都在旧实例校验候选设置，返回已有 `params.*` 冷叶字段的非空补丁。只有目标配置和逻辑链版本递增。相同补丁也会重新初始化。成功后暂停，`resume` 仍重置上下文。Docker 生命周期由 RC 管理。

候选校验被拒时原实例继续使用。局部重载在旧目标退出后初始化失败，健康成员驻留，链停止推理。`status.retry_reload_instance` 指示失败目标。修复文件后在本地 TUI 执行 `policy <精确实例ID> retry`，按已校验的候选配置重新初始化；显式保存只使用最近生效的配置。整组重载初始化失败按整组清理，使用 `policy switch <来源>` 恢复。

MCP 持有控制权时可用 `retry_reload(instance_id=...)` 或 `command("policy <实例> retry")` 冷重试同一失败候选，不能使用来源名代替重复实例。成功和失败均不抢夺 MCP 控制权。TUI 的重载、重初始化及重试仍要求 policy 拥有控制权。MCP 生命周期变更在预检通过后取消手动运动并保持当前目标；预检失败保留原状态。所有延迟回包和提交都必须通过控制代次、worker、实例、配置版本及动作代次复核。

`effect: reinitialize_policy` 复用目标容器、host、执行 PID 和已导入模块。handler 返回 `{}` 表示沿用生效配置，或返回已有 `params.*` 冷叶字段的补丁。相同值也会重建 context。目标已加载模块先运行可选的 `validate_config(config)`。校验拒绝时，原 context、动作代次和正在运行的任务均保留。校验通过后 RC 暂停整链、清空动作缓存并等待各成员及外部 reader 确认图像释放。随后 SDK 排空目标推理、观测、命令和 monitor 回调，调用 `release(old_context)`，然后以新绑定调用 `initialize(config, *, runtime)`。其他实例的 context 继续驻留。Python 模块级变量跨初始化保留，轮次或配置相关的状态应放在 context 中。

旧 `release` 可使用此前保存的 `runtime.globals`。回调退出后，旧 runtime 的资源登记和全局写入失效。释放时请自行结束创建的后台任务。`initialize` 可以正常返回 `None`，初始化抛错时也可能需要 `release(None)` 收尾。新 context 通过确认后，RC 才提交配置、更新目标及链的版本，等待显式 `resume`。释放、初始化或最终确认失败时，先检查 `status.retry_reload_command`，修复后手动冷重建目标实例。未确认释放的图像将继续占用租约。

`policy a-0 config save` 保存该实例的有效配置到来源目录 `policy/a/`，保留 YAML 注释和顺序，并将落盘 ID 写为 a。不会创建实例目录或修改其他实例。重复来源必须指定实例，不能用 `policy a config save`。多个实例先后保存时，最后一次成功保存决定来源默认配置。链顺序通过本地 `policy manage save` 或 MCP `policy_chain(action="save", policies=[...])` 保存。

## 链、动作与租约

原生 chain：`{"schema_version":3,"kind":"chain","policies":["a","b","a"]}`。chain 自身无容器，每个环节独立实例化为 `a-0 → b → a-1`，分别初始化 context、执行进程和容器；回调收到的 `config.id` 也是实例名。只重复的策略编号，按该来源在链中的出现顺序从 0 开始；实例名最多 64 字符。`[a,a,a-0]` 产生实例名冲突，会在清理旧组前报错。链推理仍串行传递动作，pause/finish 来源、命令回执和外部 reader 都保留对应实例身份。

显式切链和 `reload_all_policy` 销毁整个旧组。`reload_policy` 保留其余成员和链中位置。显式 policy switch 或重新应用链从来源默认配置创建新快照；实例命令引发的重载保留其他成员当前快照。链 YAML 始终保存来源名和顺序，不持久化每个环节的参数。SDK 的 `PolicyChain` 是另一个层次：在一个 worker 内组合模块；`runtime.globals` 继续是显式共享状态。

只有控制线程发送机器人动作和修改动作缓存。完整目标成功发送才消费；迟到推理、旧实例命令与旧版本回包不能越过切换屏障。策略失败不收回 MCP 已持有的控制权。

图像使用只读 POSIX fd/mmap，RPC 只传描述符。回调结束必须释放所有视图，禁止将数组或切片保存到 context、全局变量或后台线程；需要长期保留时 copy。租约占用的槽不会覆盖。共享内存尺寸与应用帧池容量都应匹配实际相机。

外部模型/HTTP 服务必须登记 owner、instance 与图像 reader，按协议明确释放。SDK worker 树退出证明、容器 CID/代次退出证明、外部 owner/instance 回执用途不同；本地容器删除不能替代外部释放。断连、socket 消失、CLI 超时均不是证明。

## 生命周期失败

默认策略和所有重载成功后保持暂停；显式 resume 才驱动推理。policy switch 默认不复位，--reset 才运动。release 超时由监督层和容器层兜底，完整证明允许后续初始化，但强制事实保留为非零退出。无法证明时进入 BLOCKED_CLEANUP，不启动新组、不回收未知 reader。

创建前保存意图，取消/超时后按本 generation 可信标签核对全部资源。所有清理均受同一绝对期限限制。容器 restart 固定 no；恢复必须通过统一生命周期入口，不自动复用旧 session 或回滚旧模型。

## 参数提示与路径补全（0.15）

schema 3 的 `tui_commands.args` 兼容 `[str, int]`，也允许每个参数使用对象声明。提示在加载清单时读取，不导入模型，不执行自定义补全函数。

```yaml
tui_commands:
  switch-model:
    handler: switch_model
    help: 切换模型配置
    args:
      - name: config_path
        type: path
        help: 选择模型配置文件
        root: .
        kind: file
        suffixes: [.json]
    effect: reload_policy

config_hints:
  help: 修改当前实例配置
  settings:
    params.task:
      type: str
      help: 任务描述
    execution.mode:
      type: enum
      choices: [sync, async]
      help: 推理调度模式
    params.model_config_path:
      type: path
      root: .
      kind: file
      suffixes: [.json]
      help: 模型配置文件
```

| 字段 | 含义 |
| --- | --- |
| `name` | 参数显示名称，省略时按类型或设置项名称生成 |
| `type` | `str`、`int`、`float`、`bool`、`enum`、`path` 或 `json` |
| `help` | 参数说明 |
| `choices` | 候选值，保留声明顺序；enum 必须提供非空列表 |
| `root` | 路径列举根目录，默认 `.`；只用于 path |
| `kind` | `file`、`directory` 或 `any`，默认 `any` |
| `suffixes` | 文件后缀列表；目录始终保留以便继续导航 |

`str/int/float/bool` 的执行转换保持不变，`enum/path/json` 作为字符串传给 handler，业务验证由策略完成。MCP 能力查询保留基础 `args` 类型列表，同时提供 `parameters` 完整声明及 `config_hints`。例如 path 参数的基础类型仍为 `str`。

`config_hints.settings` 的键是 `policy <实例> config set` 的设置项候选；键不能为空或含空白，但不要求点路径语法。选中设置项后提供对应值提示。未知项和类型不符只显示提示，不阻止设置；`configure(context, config, key, value, callback)` 继续收到未经提示系统转换的 key 和原始 value。声明属于冷元数据，修改后通过正常运行组重建加载。

policy 相对 root 以来源目录为基准，绝对 root 是容器内路径。相对候选带 root 前缀，绝对候选保持绝对路径，handler 沿用原路径解释方式。补全只列目录项名称和类型，不读取文件内容；根目录外的 `..` 与符号链接不提供候选，手动输入仍由 handler 校验。文件名含空格、中文、引号或反斜杠时，TUI 按当前位置的引用形式插入。

只读查询由目标 host 独立后台任务处理，不经过执行 worker，也不占用推理、暂停和退出流程。输入防抖 100 ms，查询期限与缓存均为 1 秒，最多 200 项；每个实例一个在途请求和最新待查请求。查询绑定实例和 host 身份，旧输入或旧实例回包丢弃。无法查询时只显示说明，不自动启动或重建容器。
