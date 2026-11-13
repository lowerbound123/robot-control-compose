# 策略作者规范

RC / SDK 0.18 提供显式 `Table`、`TableOutput` 和 `runtime.log_table()`；manifest 仍使用 schema 3。表格是附加展示信息，命令的业务 JSON、普通 `print()` 与未包装的返回值保持原有语义，见下文“结构化表格输出”。

SDK 0.17 保留公共配置字段 `execution.on_connector_switch`，默认 `reinitialize_policy`，可选 `reset`。所有策略从初始化时建立可重绑定的context身份，不再需要声明重初始化命令才能参与connector切换。reset必须结束旧后台请求、清空旧机器人状态；release/initialize沿用旧绑定释放、新绑定初始化的语义。详见 [connector 热切换](connector-switching.md)。

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

业务模块放在 `policy/<来源>/src/`，专用模型源码放在本目录 `vendor/`，构建输入、校验/迁移脚本和文档也归本来源。每个来源独立持有自己的实现，不从仓库级业务包或兄弟 policy 目录导入；公共 SDK 是基础接口，继续由管理器提供。实际权重和下载缓存可外部挂载。复制策略时使用独立的业务模块命名空间，避免同一验收进程导入两个来源时串用模块。

相对 bind source 以策略目录为基准，由框架转换到宿主路径。Compose env 为本目录的 `deployment.env`。需要外部目录时写清宿主路径并提前创建。发现只读声明与文件是否存在，不 import main.py；新 Python 语法只由目标容器预检。

非 external 网络由管理器按运行实例项目重命名并独立清理，包括 Compose 自动生成及显式 `name` 的网络；其余网络选项保留。需要跨实例共用的已有网络应声明 `external: true`，其名称保持原配置，管理器不负责创建或清理。

策略有独立实现包时，在自己的 Compose 中声明来源。例如：

```yaml
    environment:
      PYTHONPATH: /workspace/policy/my_policy/src
      RC_SOURCE_MODULES: '{"my_policy":"/workspace/policy/my_policy/src"}'
    volumes:
      - type: bind
        source: ${RC_HOST_PROJECT_ROOT}/policy/my_policy/src
        target: /workspace/policy/my_policy/src
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

`effect: update_config` 提交热配置补丁。handler 只验证并返回manifest 白名单内 `params.*` 或 `execution.on_connector_switch` 叶字段的非空补丁，不直接修改 context 或配置。RC 使用现有 SDK `update_config` 事务，在双端确认后更新目标配置及逻辑链版本；配置对象、context、模型、进程和容器保持不变。TUI 调用保留原运行/暂停状态，MCP 调用仍需先 take，完成后保持 MCP 控制权和 policy 暂停。链中必须解析为明确实例，其他成员的配置与版本不变；`policy example-1 set-message "新消息"` 展示该用法。命令回包与 ACK 提交都复核 worker、配置版本、控制权及动作代次；未确认或失效的提交沿用热更新故障清理，不自动重试。该 effect 通过 `tui_command_effects_v2` 双端协商，需同步更新 RC/SDK。

`effect: reload_policy` 只重建命令目标实例的容器、host 和执行进程。`effect: reload_all_policy` 重建整个活动运行组。两者的 handler 都在旧实例校验候选设置，返回已有 `params.*` 冷叶字段的非空补丁。只有目标配置和逻辑链版本递增。相同补丁也会重新初始化。成功后暂停，`resume` 仍重置上下文。Docker 生命周期由 RC 管理。

候选校验被拒时原实例继续使用。局部重载在旧目标退出后初始化失败，健康成员驻留，链停止推理。`status.retry_reload_instance` 指示失败目标。修复文件后在本地 TUI 执行 `policy <精确实例ID> retry`，按已校验的候选配置重新初始化；显式保存只使用最近生效的配置。整组重载初始化失败按整组清理，使用 `policy switch <来源>` 恢复。

MCP 持有控制权时可用 `retry_reload(instance_id=...)` 或 `command("policy <实例> retry")` 冷重试同一失败候选，不能使用来源名代替重复实例。成功和失败均不抢夺 MCP 控制权。TUI 的重载、重初始化及重试仍要求 policy 拥有控制权。MCP 生命周期变更在预检通过后取消手动运动并保持当前目标；预检失败保留原状态。所有延迟回包和提交都必须通过控制代次、worker、实例、配置版本及动作代次复核。

`effect: reinitialize_policy` 复用目标容器、host、执行 PID 和已导入模块。handler 返回 `{}` 表示沿用生效配置，或返回已有 `params.*` 冷叶字段的补丁。相同值也会重建 context。目标已加载模块先运行可选的 `validate_config(config)`。校验拒绝时，原 context、动作代次和正在运行的任务均保留。校验通过后 RC 暂停整链、清空动作缓存并等待各成员及外部 reader 确认图像释放。随后 SDK 排空目标推理、观测、命令和 monitor 回调，调用 `release(old_context)`，然后以新绑定调用 `initialize(config, *, runtime)`。其他实例的 context 继续驻留。Python 模块级变量跨初始化保留，轮次或配置相关的状态应放在 context 中。

旧 `release` 可使用此前保存的 `runtime.globals`。回调退出后，旧 runtime 的资源登记和全局写入失效。释放时请自行结束创建的后台任务。`initialize` 可以正常返回 `None`，初始化抛错时也可能需要 `release(None)` 收尾。新 context 通过确认后，RC 才提交配置、更新目标及链的版本，等待显式 `resume`。释放、初始化或最终确认失败时，先检查 `status.retry_reload_command`，修复后手动冷重建目标实例。未确认释放的图像将继续占用租约。

`policy a-0 config save` 保存该实例的有效配置到来源目录 `policy/a/`，保留 YAML 注释和顺序，并将落盘 ID 写为 a。不会创建实例目录或修改其他实例。重复来源必须指定实例，不能用 `policy a config save`。多个实例先后保存时，最后一次成功保存决定来源默认配置。链顺序通过本地 `policy manage save` 或 MCP `policy_chain(action="save", policies=[...])` 保存。

## RC 调用调度与 policy 推理判断

所有 policy 的外部调用模式由 RC 的 `execution.mode`（sync/async）控制，调用间隔由 `execution.call_every_actions` 控制；合法值统一由 SDK/RC 校验，policy 不应再限制必须 sync 或间隔必须为 1。只修改这两个字段且不请求生命周期动作时，配置事务保留动作缓存、动作代次和已加载 context，仍执行读者屏障与配置版本确认。

调用 policy 不等于启动模型推理。policy 可根据输入剩余 chunk 自行选择原样返回或推理；需要推理的回调可以等待结果，外部 async 的 RC 继续执行缓存。返回 chunk 保持请求时的起点，RC 负责裁掉请求到回包期间已消费的前缀及保护已提交目标。独立 `observe` 可在推理期间采样历史。

## 可选动作计数标记

`ActionChunk(actions, modes, clipped=...)` 可附带与动作数组形状相同的布尔矩阵，表示 policy 已对哪些目标维度执行过裁剪。SDK 复制并验证标记，通过结果的 `action_annotations` 传递；标记只用于统计，不要求 RC 改变动作或再次限幅。

RC 随动作一起裁掉过时标记并保留已提交目标的标记，只在完整发布确认后累计。无标记的原样返回保留相同目标的来源和标记；原生链中透传的标记保留原实例/context 归属，发生变化的前缀不继续冒用旧标记。统计通过 `runtime.action_stats` 按实例 ID 返回，包含 `context_id`、`clipped_actions` 和 `clipped_dimensions`。policy 应校验 context 绑定，缓存统计供 monitor 只读展示；普通 reset 保留，重新初始化的新 context 从零开始。

## 可选配置导出

策略可提供纯数据 `export_config(context, config)`，返回 `{"policy": manifest_projection, "settings": display_fields, "files": [{"path": "model.json", "data": model_snapshot}]}`。SDK 在初始化、配置提交和重新初始化确认时导出，RC 缓存结果供 `config show/save` 使用；查询和保存不临时调用策略。hook 不读写文件、不执行推理，不修改传入的配置或 context。

`policy` 保持身份、execution 和元数据原值，只允许从 params 省略内部运行快照；`settings` 不得覆盖 manifest 顶层字段。`files` 为最多 32 个唯一 JSON 目标，内容必须是有限、普通 JSON 对象。优先使用来源目录相对路径；本地 host 使用源副本时可给出原来源内绝对路径，RC 始终检查目标不越出来源目录，拒绝符号链接、路径穿越和与 manifest 重复的目标。

`config save` 在生命周期线程复核目标实例、版本和动作代次后，将保存投影写入来源 manifest，并保存附属 JSON；先预写临时文件，再原子替换，普通替换失败回滚已替换文件。失败及回滚失败会明确报告，保存不更新其他已加载实例，也不替换当前内存配置或版本。未实现 hook 的策略沿用原有保存方式。该能力需配套更新 RC 与 SDK。

配置 set 的成功回执额外包含 `configuration_effect`（`ordinary`、`reset` 或 `reinitialize`），说明此次配置实际采用的生命周期方式。

## 结构化表格输出（0.18）

命令 handler 可以显式返回 `TableOutput(result=..., tables=[...])`。`result` 放完整业务返回值，`tables` 放按显示顺序排列的表格。下面复用 example 已有的 `monitor(context)` 快照；原命令 `example(context)` 继续直接返回该字典：

```python
from rc_policy_sdk import Table, TableOutput

def table_stats(context):
    snapshot = monitor(context)
    return TableOutput(
        result=snapshot,
        tables=[Table(
            title="实例统计",
            columns=["实例", "推理次数", "观测次数", "已释放"],
            rows=[[snapshot["instance"], snapshot["calls"],
                   snapshot["observations"], bool(snapshot["released"])]],
        )],
    )
```

在 schema 3 的 manifest 中按普通命令注册即可，无需新的命令类型：

```yaml
tui_commands:
  table-stats:
    handler: table_stats
    args: []
    help: 以表格查看实例统计，业务回执保留完整 JSON
    timeout_s: 5
```

表格模型、校验和纯文本投影只使用 Python 标准库，策略无需导入 Rich 或 Textual；SDK 的依赖仍只有 NumPy 与 posix-ipc。`Table` 的 `title` 是字符串，`columns` 至少包含一个字符串，`rows` 中每行长度必须与列数相同；空行集合 `rows=[]` 合法。单元格和 `result` 必须是有限 JSON 数据，支持字符串、数字、布尔、null、数组与对象，不接受 NaN、Infinity 或任意 Python 对象。`TableOutput` 必须显式提供 `result` 且至少含一张 `Table`。构建时生成独立数据副本；既有 wire 帧大小和日志队列预算继续适用，不能靠表格绕过限制。

传输展示文档是 `{"schema_version":1,"tables":[{"title":...,"columns":[...],"rows":[...]}]}`，与原始 `result` 分开。RC 只有在命令最终成功后才发布表格；带 `update_config`、`reinitialize_policy` 或冷重载 effect 时，仍用原始 `result` 校验和提交补丁，失败或取消不显示成功表格。operation 回执和 HTTP MCP 的业务 `result` 不增加包装层，也不包含表格文档。未包装的字典即使有 `tables` 或 `table_document` 键，也仍按普通 JSON 处理。

主动诊断在 `initialize`、`policy` 或 `observe` 回调中使用当前 context 的 runtime：

```python
# 在 initialize(config, *, runtime) 建好 context 后，可先于 ready 输出一次。
if config["params"].get("table_logs", False):
    runtime.log_table(table_stats(context), level="info")

# 在 policy / observe 中，先更新该回调自己的计数，再按需要采样。
if config["params"].get("table_logs", False) and count % interval == 0:
    accepted_locally = runtime.log_table(table_stats(context), level="debug")
```

`level` 支持 trace、debug、info、warning、error、critical。返回的 bool 仅表示本地发送队列是否收下这条日志，不是 RC 已收到、已显示或已持久化的确认。调用会同步校验并复制数据，但不等待 socket 发送或 ACK。worker、host 和客户端各阶段都使用有界的尽力发送队列；压力下允许丢弃，丢失提示只汇总数量，可见日志可能出现缺口。调用方应限制表格大小和频率，不要因返回 false 重试、阻塞推理或保留图像视图。

表格能力通过 `table_output_v1` 协商，不能在 manifest 中自行打开。旧端未支持时，新命令返回或 `runtime.log_table()` 会明确报需要升级 RC/SDK 的错误，不会静默转换成普通成功结果。使用前同步更新 RC 和挂载的 SDK 源码，并通过正常生命周期重建运行组；依赖未变时无需为表格单独安装 UI 库或重建模型依赖镜像。

事件中的实例、来源、runtime、session、context 与配置版本由框架绑定。重复来源的 `example-0` 与 `example-1` 分别产生自己的日志与复制内容，不会按来源合并。原进程重初始化时，新的 initialize 可以在 ready/最终提交前输出合法的新 context 日志；旧绑定交接完成后，旧 runtime 的 `log_table()` 会被拒绝。普通配置提交保留 config 引用，日志携带当前提交版本；不要跨 context 保留和复用旧 runtime。

TUI 在原日志区展开这些表格，多表纵向排列。只有最后一列换行；前列过宽、含换行或窗口过窄时，整表改为逐条纵向显示，保留完整字段。`0`、`false`、`null` 和空字符串各自保留，空表显示“无记录”。文字中的 Rich 标记和终端控制字符按字面处理。整条“复制”得到完整原始业务 JSON（沿用既有敏感字段脱敏），拖选复制逻辑正文，不含边框、补齐空格或软换行。headless 和日志文件保留可读纯文本投影；日志事件同时保留 `table_document` 与 `copy_json`。

内置 `connector list`、`policy list`、`status`、`connector status` 和 `record status` 使用同一展示；`help` 仍保留原 `HelpDocument`、完整帮助文本与复制行为。普通配置、global、MCP 回执和 `print()` 不会自动变成表格。完整规则见[统一命令树](commands.md)。

example 的 `table-stats` 展示实例、回调和已脱离图像映射的相机摘要，业务值与 `example` 命令的完整快照一致。`params.table_logs` 默认 false，旧本机 manifest 缺少该字段也按 false 处理；显式开启后初始化输出一次，policy/observe 分别按自己的计数和 `log_every_n_calls` 采样。通过 `config set params.table_logs true|false` 普通提交，不触发 reset/reinitialize；下一次初始化才会产生新的初始化表。已有私有 manifest 由用户核对并更新[模板](../policy/example/policy.yaml.example)中的 `table-stats` 注册与帮助声明，setup 不覆盖本机文件。

## 链、动作与租约

原生 chain：`{"schema_version":3,"kind":"chain","policies":["a","b","a"]}`。chain 自身无容器，每个环节独立实例化为 `a-0 → b → a-1`，分别初始化 context、执行进程和容器；回调收到的 `config.id` 也是实例名。只重复的策略编号，按该来源在链中的出现顺序从 0 开始；实例名最多 64 字符。`[a,a,a-0]` 产生实例名冲突，会在清理旧组前报错。链推理仍串行传递动作，pause/finish 来源、命令回执和外部 reader 都保留对应实例身份。

显式切链和 `reload_all_policy` 销毁整个旧组。`reload_policy` 保留其余成员和链中位置。显式 policy switch 或重新应用链从来源默认配置创建新快照；实例命令引发的重载保留其他成员当前快照。链 YAML 始终保存来源名和顺序，不持久化每个环节的参数。SDK 的 `PolicyChain` 是另一个层次：在一个 worker 内组合模块；`runtime.globals` 继续是显式共享状态。

只有控制线程发送机器人动作和修改动作缓存。完整目标成功发送才消费；迟到推理、旧实例命令与旧版本回包不能越过切换屏障。策略失败不收回 MCP 已持有的控制权。

图像使用只读 POSIX fd/mmap，RPC 只传描述符。回调结束必须释放所有视图，禁止将数组或切片保存到 context、全局变量或后台线程；需要长期保留时 copy。租约占用的槽不会覆盖。共享内存尺寸与应用帧池容量都应匹配实际相机。

外部模型/HTTP 服务必须登记 owner、instance 与图像 reader，按协议明确释放。SDK worker 树退出证明、容器 CID/代次退出证明、外部 owner/instance 回执用途不同；本地容器删除不能替代外部释放。断连、socket 消失、CLI 超时均不是证明。

## 生命周期失败

默认策略和所有重载成功后保持暂停；显式 resume 才驱动推理。policy switch 默认不复位，--reset 才运动。release 超时由监督层和容器层兜底，完整证明允许后续初始化，但强制事实保留为非零退出。无法证明时进入 BLOCKED_CLEANUP，不启动新组、不回收未知 reader。

创建前保存意图，取消/超时后按本 generation 可信标签核对全部资源。所有清理均受同一绝对期限限制。容器 restart 固定 no；恢复必须通过统一生命周期入口，不自动复用旧 session 或回滚旧模型。

## 静态帮助元数据（0.17）

简短说明继续使用 `help: 字符串`。policy 顶层（包括原生 chain）、每条 `tui_commands` 和 `config_hints` 都可声明可选的 `help_details`，用于分组、长说明、执行影响和示例：

```yaml
help: 设置任务消息
help_details:
  group: 任务设置
  description: 修改当前实例的任务消息，保留执行进程。
  sections:
    - title: 执行影响
      body: 配置提交成功后生效，不重置统计。
  examples:
    - title: 设置中文消息
      command: 'policy <实例> set-message "整理桌面"'
```

所有字段均可省略，但出现时必须符合下表；`sections`/`examples` 每项的两个字段必须完整。未知字段、错误类型、控制字符和超限内容在加载 manifest 时拒绝。文字中的方括号、引号与 JSON 原样显示，不作为 Rich 标记、表达式、回调或模板执行。

| 字段 | 类型与限制 |
| --- | --- |
| `group` | 普通文本，最多 128 字符 |
| `description` | 普通文本，最多 16384 字符 |
| `sections` | 最多 32 项；每项 `title` 最多 256 字符、`body` 最多 16384 字符 |
| `examples` | 最多 64 项；每项 `title` 最多 256 字符、`command` 最多 4096 字符 |

单份 `help_details` 的 JSON 文本总长度最多 131072 字符。仅允许换行与制表符作为文本控制符，不接受 ANSI 转义。`<实例>` 是唯一会替换的占位符：RC 只在已加载的确切实例帮助中替换它。未加载来源保留 `<实例>` 并显示说明；重复来源按 example-0、example-1 等实例分别呈现。其他 `${...}`、花括号与尖括号文本不插值。帮助不会插入实时配置值、环境变量或密钥。

参数说明从现有 `args` 和 `config_hints.settings` 生成；`help_details` 只丰富展示，不能成为业务校验，也不会改变 `configure(context, config, key, value, callback)` 接收原始文本的语义。这些声明是冷元数据，普通配置提交不能修改，正常执行组重建时才加载新的声明。

`help` 显示分组总览，`help config rc set` 等完整 token 路径显示详情；`policy help [来源或实例] [命令]` 查询静态 manifest，永不导入或启动未运行模型；命令位置填写 `config` 可只查看配置说明。原生 chain 也显示简介、成员顺序及各成员声明；未加载链显示计划实例身份，仍保留示例占位符。完整的三处声明见 `policy/example/policy.yaml.example`。

所有帮助入口共用 JSON 可序列化的 `HelpDocument`（`schema_version: 1`），包括标题、摘要、用法、章节、命令目录、参数和示例。日志 `message` 保存完整纯文本，并附 `help_document` 供 TUI 排版；headless 和文件不含 ANSI。HTTP MCP 的 `content.text` 是同一可读文本，`structured_content` 保留原能力字段并附 `help_document`。普通状态与操作回执仍使用结构化 JSON；文本批次中的帮助不改变批次回执结构。

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
