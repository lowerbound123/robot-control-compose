# 全局共享状态

RC/SDK 0.11 提供跨 policy 的共享字典。RC 统一提交写入，各 worker 从独立的只读 POSIX 共享区读取完整快照。使用已有 IPC anchor 与 host 连接，不增加容器。旧策略不改回调签名也能继续运行；使用此功能时必须更新 RC 和对应 policy 镜像。

## Python 接口

```python
from rc_policy_sdk import GlobalStateConflict

def initialize(config, *, runtime):
    shared = runtime.globals
    shared.update({"task": {"phase": "ready"}, "ready": True})
    return {"shared": shared}

def policy(context, state, action_chunk, images, config, *, runtime):
    shared = context["shared"]
    task = shared.get("task", {})
    version, snapshot = shared.snapshot()
    try:
        shared.patch({"attempt": snapshot.get("attempt", 0) + 1},
                     expected_version=version)
    except GlobalStateConflict:
        pass  # 别的写者已提交；重新读取后由业务决定是否重试。
    return action_chunk if len(action_chunk) else [state["qpos"]]

def reset_context(context):
    context["shared"]["reset_requested"] = True

def release(context):
    if context is not None:
        context["shared"]["released"] = True
```

RC 代码使用 `controller.globals`。代理属于整个 session，可以保存在 context 中供推理、观测、命令或 release 使用；不能在 session 退出之后继续使用。读取返回副本：`shared["task"]["phase"] = "done"` 只改临时副本，应重新赋值整个 `task` 或使用 `patch()`。

`g[key]`、`get()`、`snapshot()` 读取当前快照；`g[key] = value` 新增或替换；`del g[key]` 删除不存在的键时报 KeyError。`update(mapping)` 原子合并顶层字段；`patch(mapping, delete=(), expected_version=None)` 支持原子更新和删除，成功返回提交版本，冲突或任何无效字段都不部分修改。`snapshot()` 返回 `(version, dict)`；版本在当前 RC run 内单调递增。

值只接受内建 JSON 类型，数字必须有限，嵌套字典键必须是字符串。NumPy 数组、tuple 和模型对象不自动转换。顶层键匹配 `[A-Za-z_][A-Za-z0-9_]*`；可把一个策略的数据放在以策略名命名的嵌套字典里。

同字段以后提交的值为准，普通读后写不是事务，计数或合并请用版本条件更新。写入确认表示共享快照已提交；推理后续失败不会撤销它。超时或连接断开可能发生在提交之后，不能据此判定未写入，也不自动重试。需要每条信息必达的场景应另用消息协议。

## 终端与 MCP

```text
show global
show global task
set global.task {"phase":"grasp","attempt":1}
set global.ready true
unset global.task
```

新增字段无需 manifest 白名单。嵌套数据按整个顶层字段替换。TUI/headless 共用操作回执，只有 succeeded 才表示提交成功。MCP 可直接 `show(scope="global")` 或 `command("show global task")`；修改仍要求 take，归还或代次变化后排队的旧写请求不能提交。全局字段本身不触发运动、不修改配置或控制权。

## 文件与冷配置

RC 配置可加入：

```json
"global_state": {"path":"global.env","max_bytes":1048576,"timeout_s":2.0}
```

路径相对 RC 配置文件目录，标准 Compose 使用 `configs/global.env`，复用 configs 的目录挂载。自定义路径须位于 RC 可写且已挂载的目录中。默认整个 JSON 字典容量 1 MiB，允许 2 字节到 3 MiB，不能在线扩容；超限整次修改失败。`timeout_s` 为单次锁/写入等待预算。所有设置均为冷参数。

```text
# 初始状态
task={"phase":"idle","attempt":0}
ready=false
label="中文与空格；$HOME 保持原样"
```

文件为 UTF-8，每行 `key=<严格JSON>`，接受空行和整行注释，不执行 shell、不插值、不读 os.environ。字符串需要 JSON 双引号；换行需转义。重复键和非法数据会在连接机器人前阻止启动。文件缺失时从空字典开始；setup 只在缺失时创建，不覆盖现有内容。

运行期间不热读文件，直接修改文件可能被退出快照覆盖。policy 切换、重载、暂停不会清空字典；RC 正常退出或处理 SIGINT/SIGTERM 时，等待 release 收尾后冻结写入口，再通过子进程执行临时文件、fsync、原子替换。全部字段保存，注释和排列按规范化输出重写。保存与清理共享原来的绝对退出期限；失败或结果无法确认返回非零，文件保持完整旧版或完整新版。

SIGKILL/断电无法保证保存本轮数据。下一次 RC 从文件初始化新共享区，不从孤儿共享内存恢复业务状态。只有旧运行组退出得到证明后才回收它的共享资源。实际 global.env 与临时保存文件不进入 Git 或镜像；自定义文件名也应自行排除。
