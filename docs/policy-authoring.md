# 策略作者规范

## 目录身份与声明

每个可执行策略独占一个 host 容器和受监督执行进程。目录名必须匹配 `[a-z0-9][a-z0-9_-]{0,47}`，manifest 的 id 可省略或与目录一致。没有额外 host/socket 表。

```text
policy/example/
  policy.json
  main.py
  compose.yaml
```

```json
{
  "schema_version": 3,
  "execution": {"mode": "sync", "call_every_actions": 1, "control_mode": "qpos"},
  "params": {"gain": 1.0},
  "hot_reloadable": ["params.gain"]
}
```

```yaml
services:
  policy:
    image: your-prepared-policy-image:tag
```

image 包含本项目兼容 SDK 与自己的依赖，并提供 `python` 命令。可添加 build、环境、GPU 和 bind mounts；部署配置归本目录。管理器注入唯一 host ID、socket、run/generation、用户身份、共享 rpc 卷、IPC anchor 与只读本策略目录。不要声明 sidecar、host PID、Docker socket 或 privileged。

相对 bind source 以策略目录为基准，由框架转换到宿主路径。Compose env 为本目录的 `deployment.env`。需要外部目录时写清宿主路径并提前创建。发现只读声明与文件是否存在，不 import main.py；新 Python 语法只由目标容器预检。

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

## 命令

```json
{
  "tui_commands": {
    "set-gain": {"handler": "set_gain", "args": ["float"], "help": "设置任务参数", "timeout_s": 5}
  }
}
```

在 main.py 声明 handler，业务验证由策略完成。使用 `help example` 查看静态声明，即使尚未运行也可以查看；执行 `policy example set-gain 0.5` 需要该实例已运行。链内同名裸命令不再覆盖前一个成员，使用显式目标或 `<policy_id>-<command>`。策略命令不自动向 MCP 开放。

`effect=reload_policy` 的 handler 返回已有 `params.*` 冷叶字段补丁。例如 Qwen 的 `switch-model` 在旧实例验证模型配置，然后由 RC 执行完整清旧启新事务。不得从 handler 调 Docker。部署参数通过本目录修改，在下次 switch 重新解析。

## 链、动作与租约

原生 chain：`{"schema_version":3,"kind":"chain","policies":["a","b","a"]}`。chain 自身无容器，重复成员只初始化一次，复用 context。`[a,b]` 切 `[b,c]` 时旧 a、b 都销毁。SDK 的 `PolicyChain` 是另一个层次：在一个 worker 内组合模块。

只有控制线程发送机器人动作和修改动作缓存。完整目标成功发送才消费；迟到推理、旧实例命令与旧版本回包不能越过切换屏障。策略失败不收回 MCP 已持有的控制权。

图像使用只读 POSIX fd/mmap，RPC 只传描述符。回调结束必须释放所有视图，禁止将数组或切片保存到 context、全局变量或后台线程；需要长期保留时 copy。租约占用的槽不会覆盖。共享内存尺寸与应用帧池容量都应匹配实际相机。

外部模型/HTTP 服务必须登记 owner、instance 与图像 reader，按协议明确释放。SDK worker 树退出证明、容器 CID/代次退出证明、外部 owner/instance 回执用途不同；本地容器删除不能替代外部释放。断连、socket 消失、CLI 超时均不是证明。

## 生命周期失败

默认策略和所有重载成功后保持暂停；显式 resume 才驱动推理。switch 默认不复位，--reset 才运动。release 超时由监督层和容器层兜底，完整证明允许后续初始化，但强制事实保留为非零退出。无法证明时进入 BLOCKED_CLEANUP，不启动新组、不回收未知 reader。

创建前保存意图，取消/超时后按本 generation 可信标签核对全部资源。所有清理均受同一绝对期限限制。容器 restart 固定 no；恢复必须通过统一生命周期入口，不自动复用旧 session 或回滚旧模型。
