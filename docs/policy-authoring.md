# Policy 编写与多容器部署

## 配置与入口

本项目的 manifest 和 RC 配置均使用 `schema_version: 2`。旧版本配置必须先迁移，运行时会明确拒绝 `conda_env`、`conda_executable`。

- policy manifest：`conda_env` 改为 `policy_host`。
- RC：`conda_executable` 改为 `policy_hosts`，每个 host 配置一个绝对 UDS 路径。
- 每个 policy 有唯一 `id`；host 按 manifest 的 ID 查找目录。推荐目录与 ID 同名。
- 主框架、对应 host 均须挂载同一套 policy 文件；主框架需要写 manifest 时挂载整个目录，host 可只读挂载。

最小目录：

```text
policy/hold/
  policy.json
  main.py
```

`hold/policy.json` 是可运行模板。`main.py` 必须在模块顶层声明四个同步函数：

```python
from pathlib import Path
from .helper import normalize  # 辅助模块按相对导入组织

ASSET_ROOT = Path(__file__).parent

def initialize(config):
    return {"config": config}

def policy(context, state, action_chunk, images, config):
    return normalize([state["qpos"]])

def reset_context(context):
    pass

def release(context):
    pass
```

RC 只检查 manifest 与入口存在。host 使用其镜像内的 Python（hold 为 3.14，Qwen 为 3.11）对入口做 AST/编译预检和四函数检查，预检不 import 或执行顶层代码；辅助模块与依赖在实际加载时检查。各实例是独立模块包，拥有独立 context、配置、推理队列。不要修改全局 `sys.path` 或 `os.chdir()`，同 host 的第三方依赖由一个解释器共享。

已有 SDK 的 `runtime`、`observe`、TUI 命令、外部资源与热更新接口继续可用。只对 manifest `hot_reloadable` 中允许的叶路径做热更新，其他字段冷重载。`runtime.pause(reason)` 只能在当前推理回调线程调用，成功结果才会携带暂停请求。

## 同容器多个 policy

将 hold 复制为 `hold_a`、`hold_b`，分别修改 manifest 的 `id`，二者都设为 `"policy_host": "hold"`。RC 保留一个映射：

```json
{
  "policy_hosts": {
    "hold": {"socket": "/run/robot-control/hold.sock"}
  }
}
```

使用原有链编辑器组装 `hold_a → hold_b → hold_a`，再应用链。host 在同一个执行进程加载 A、B；A 初始化一次，两次调用复用同一个 context。整链共享一次图像快照。UI 中 `help` 查看链编辑命令；链的保存和应用方式与迁移前相同。

## 不同容器

为 B 指定 `"policy_host": "second"`，并在 RC 的 `policy_hosts` 增加：

```json
"second": {"socket": "/run/robot-control/second.sock"}
```

添加 `compose.policies.yaml`：

```yaml
services:
  policy-second:
    user: "${RC_UID:?Run ./scripts/setup-user.sh first}:${RC_GID:?Run ./scripts/setup-user.sh first}"
    environment:
      HOME: /tmp/robot-control
      XDG_CACHE_HOME: /tmp/robot-control/.cache
    tmpfs:
      - /tmp/robot-control:uid=${RC_UID},gid=${RC_GID},mode=0700
    image: robot-control-compose-policy:local
    ipc: service:ipc-anchor
    depends_on:
      rpc-init:
        condition: service_completed_successfully
      ipc-anchor:
        condition: service_started
    command: ["--host-id", "second", "--socket", "/run/robot-control/second.sock", "--policy-root", "/workspace/policy"]
    volumes:
      - rpc:/run/robot-control
      - type: bind
        source: ./policy
        target: /workspace/policy
        read_only: true
        bind: {create_host_path: false}
    healthcheck:
      test: ["CMD", "python", "-m", "rc_policy_sdk.host", "--healthcheck", "--host-id", "second", "--socket", "/run/robot-control/second.sock", "--policy-root", "/workspace/policy"]
      interval: 2s
      timeout: 3s
      retries: 20
    stop_grace_period: 45s
  robot-control:
    depends_on:
      policy-second:
        condition: service_healthy
```

使用 `docker compose -f compose.yaml -f compose.policies.yaml up -d`。B 可以使用另外构建的镜像安装不同依赖；保持相同 RPC/图像协议和 `posix-ipc==1.3.2`，Python 版本由对应镜像锁定。不要共用 socket 路径或修改 IPC anchor。跨 host 的调用顺序仍由 RC 编排。`compose.test.yaml` 提供了实际验证的双 host 拓扑。

新增 host 必须沿用同一组 `RC_UID`/`RC_GID` 并等待 `rpc-init` 完成。socket 和共享内存保留私有访问权限，不能只把 RC 改为普通用户而让 host 继续以 root 运行。上面的 HOME/cache 配置保证任意数字 UID 都有可写目录，不依赖镜像内同名账户；自定义宿主输出挂载也应预先由该用户创建。

复用环境不必共用运行进程：Qwen 的两个服务引用同一个 `robot-control-compose-qwen35:local` 镜像，各自使用独立 host 和 UDS。镜像只构建一次，模型、context、动作缓存和故障监督各自独立。配置见 `compose.qwen.yaml`，部署步骤见 [Qwen 教程](qwen35-policy.md)。主框架只路由 host，不负责安装包；依赖变化需重新构建相应镜像并重新创建容器。

## 图像与租约

RC 独占写入，默认每相机三个帧槽。POSIX 名称包含本次运行的随机 pool ID，描述符携带 frame ID、shape、dtype、nbytes 和时间戳。16 字节头部校验帧身份与实际长度；RPC 单消息最多 4 MiB，传描述符，不传图像字节。

policy 的数组通过只读 fd、`mmap.ACCESS_READ` 和 `np.frombuffer` 获得。回调结束后 SDK 必须关闭映射，才能返回 `images_released: true`。禁止把数组及其切片保存到 context、全局变量或后台线程；确需跨调用持有时先 `images[name].copy()`。视图逃逸会使整个执行进程失效。

未释放租约的槽不会覆盖；三个槽均被占用时丢弃新帧。尺寸变化建立新段，旧段在读者释放后回收。内存容量不足明确报错；Linux 使用 `posix_fallocate` 预留页面，避免 mmap 写入时 SIGBUS。调整 `RC_SHM_SIZE` 时还要检查应用帧池上限与图像分辨率。

## 故障、重载与退出

`runnable` 与 `has_readers` 分开处理。普通命令异常只影响该调用；硬超时、崩溃、视图逃逸影响同 host 全部 policy。RC 丢弃相关动作，保留已经建立的 MCP 接管边界，不自动重放请求、不自动重新初始化。

冷切换或重载逆序 release 唯一 policy，再按 host 停止整个执行进程树。只有旧组退出已确认，才初始化新组。重新加载后保持暂停，执行 `resume` 才恢复控制。使用 `switch hold --no-reset` 可重新加载 hold 并保留机器人当前位置；`switch hold` 沿用原有 reset 行为。

如果断连或 ACK 丢失，RC 仅允许重新连接请求清理证明，不重放初始化、推理或命令。若 host 已换实例或退出无法确认，相关帧保持冻结；停止整个 Compose 栈并重建 IPC anchor 后恢复。业务容器不要配置自动重启以绕过此边界。

正常停止先让 RC release 并清理运行组，再停止 policy host，最后停止 anchor。默认 RC/host 停止宽限分别为 90/45 秒，release 默认 30 秒（同 host 共用一次组期限）。增加 host 数量或 release 期限时相应增加 RC 宽限；超出宽限 Docker 最终会 KILL，最后通过销毁 IPC 命名空间回收未确认资源。强制清理返回非零退出码。

## LLM 离散动作 policy

新增 `llm_direction`：三路图像 + task，经 DeepSeek、自定义 OpenAI 兼容接口、OpenAI Decisions 或 Responses 决策，输出标准 epos 动作块。支持双手各选方向/开闭夹爪/noop，完成后暂停；参数、环境和链式使用见 [使用教程](llm-direction-policy.md)。

## 可选信息监控

SDK 0.8 支持 `monitor(context) -> dict[str, float | int | str]`，与推理并行、仅在面板开启时调用。必须提供快速、只读、线程安全的标量快照；生命周期操作会等待读取完成。完整契约和示例见 [Policy 信息监控](policy-monitor.md)。`monitor-` 是保留的本地命令前缀。
