# Schema 3 部署准备

## 配置和镜像

```bash
./setup.sh --policies hold
# 或准备任意合规范目录；省略 --policies 时准备全部有效策略
./setup.sh --policies my_policy,qwen35_twostage
./setup.sh --config-only
```

setup 使用宿主 Python 3.9+、venv 和 Docker CLI，无 Conda。`SETUP_PYTHON` 可选择宿主解释器；setup 在忽略的 `.setup/tools-venv` 中按版本与 hash 锁准备 YAML 工具依赖，仅首次使用或锁/解释器变化时安装，不修改全局 Python。正式 `policy.yaml` 是发现依据，`.example` 只是补齐缺失配置的模板；只有模板时目录显示 needs_config。无效目录会显示原因。通用校验不解释 provider、model.json、GPU 或 HF 业务字段，真正业务校验由目标策略执行。

setup 先合并、验证，再以原子写入更新本机文件。既有值保留，发生变更的文件备份到 `.setup/backups/`，权限为 0600。配置、密钥、输出与运行账本都被 Git 和镜像构建上下文忽略。

每个策略的 `compose.yaml` 只能有一个 `policy` 服务，必须显式指定 image。setup 对 build 声明构建镜像，直接复用已准备好的本地镜像，仅对缺少的预构建镜像执行 pull。相同 image tag 只有在 Dockerfile 内容、context、args、target 与其他有效输入一致时才能去重；不同输入报错。两个 Qwen 各有完整 build 声明与依赖锁，通过内容相同的私有依赖上下文共享镜像；单独选择 twostage 不依赖 single 的文件。

运行期间只使用准备好的本地 image ID。模型权重与外部 mount 目录应提前准备，setup 不下载权重、不访问供应商、不验证真实推理。

## YAML 主配置与手动迁移

RC 主配置使用 `rc.yaml` 或 `configs/rc.<profile>.yaml`，策略和原生链固定使用 `policy/<id>/policy.yaml`；模板对应 `.yaml.example`。RC 显式路径也接受 `.yml`：已有唯一 `.yml` 时原地使用，同名 `.yaml/.yml` 并存时报错。`model.json`、provider JSON、checkpoint 元数据、机器运行记录、状态快照、`global.env` 和部署 env 保持原格式。

旧主配置 JSON 必须手动迁移。先正常 `down`，再将原值填写到新 YAML（JSON 内容本身也是合法 YAML，可先手动改扩展名），并将旧主配置移出活动配置位置。setup 检测到旧文件或新旧并存时会报错，不会自动转换，也不会为它另建默认 YAML。实施更新不会改动本机已有配置。迁移后执行 setup 重建 RC 依赖镜像一次，再按原覆盖参数启动；policy 运行镜像无需新增 YAML 依赖。

YAML 使用 1.2 语义，数据仍限于字符串、有限数字、布尔值、null、列表和字符串键字典。重复键、多文档、YAML 1.1 指令、自定义标签、合并键和循环引用均拒绝；日期文本需加引号。普通别名按独立值进入运行时。错误只给出文件、位置与类别，不打印配置原文。

`save-config` 和 manage-policy 保存保留未改动节点的注释、键顺序、引号及多行文本样式；列表评论按值及出现次序匹配。setup 只补缺失默认键，保留当前列表和显式 null；运行时保存完整有效快照。保存前重新解析输出并严格校验类型，再逐文件原子替换。终端 `set` 和 MCP 参数仍采用 JSON 语法。宿主 `compose.sh down` 无需 YAML 库，也不会触发依赖安装。

## 依赖镜像与只读源码

所有默认部署使用依赖环境镜像加只读源码挂载。镜像只保留 Python、ROS、CUDA、系统工具、锁定的第三方依赖和两个 ROS 消息包的编译产物。RC、SDK、策略实现、Qwen vendor、项目补丁、启动/anchor 脚本、Python 仿真程序和测试代码均从宿主读取；容器启动不执行 pip 安装。镜像构建上下文仅允许依赖锁、Dockerfile 和 ROS 消息包输入。

RC 从 `/workspace/src` 导入，SDK 从 `/workspace/packages/policy-sdk/src` 导入。模型实现路径由各策略 Compose 声明，只有 Qwen 的 PYTHONPATH 包含 vendor；Pi05 使用镜像内锁定的官方 LeRobot。启动会验证声明的模块来源；目录缺失、不可读、来源错误都失败，不回退到旧安装包。所有源码 bind 都是只读且 `create_host_path: false`。字节码缓存统一写入容器 `/tmp/rc-pycache`，不会读取或写入宿主源码目录里的缓存。

| 修改内容 | 生效操作 |
| --- | --- |
| RC、SDK、基础 entrypoint 或 anchor | 正常 `down` 后重新 `up -d --no-build`，连同策略运行组一起重新创建 |
| 策略 main.py 或实现包 | `switch <当前策略>`；在链中则 `switch <当前链>` |
| Qwen vendor 或 Transformers 补丁 | 重新 switch，创建新的策略容器 |
| Python ROS 仿真程序 | 重新创建 ros-sim 容器 |
| 第三方依赖、Python/CUDA/系统环境 | 修改对应依赖声明并重建相关镜像 |
| ROS 消息定义 | 重建 RC 镜像，重新编译消息包 |

首次迁移需要一次完整构建：先用原部署的 `./scripts/compose.sh down` 正常收尾，再更新源码、执行 `./setup.sh --policies <需要的策略>`，最后使用原有 Compose 覆盖参数 `up -d --no-build`。两个 Qwen 仍按相同构建输入去重。以后普通源码修改无需 setup/build。迁移前的旧镜像仍含旧实现，不能作为“源码已外置”的验收镜像。

不会监视文件或主动替换已导入模块；运行中编辑源码不保证立即生效。冷切换仍先证明完整旧组退出，再启动新组。

## 基础设施元数据

setup 记录 `.setup/runtime.json` 和 `.setup/base.env`：部署 UUID、基础 project、rpc 卷、宿主根目录到 `/workspace` 的映射、UID/GID、实际 Docker Unix socket/GID。复制到新项目路径后重新 setup 会得到新的部署身份。不要手工复用另一部署的运行账本。

RC 镜像固定 Docker CLI 28.5.1 与 Compose 2.39.1。宿主使用支持 additional_contexts、JSON config 和 external volume 的 Compose；本轮宿主验证版本见 verification.md。

RC 读取 `/workspace/policy/<id>/compose.yaml`，但 bind source 必须是 daemon 宿主路径。管理器把相对策略目录的 bind 路径转换为宿主绝对路径。`deployment.env` 的 Compose 插值属于该策略；父进程的模型变量不会覆盖它。通用预定义变量包括 `RC_HOST_PROJECT_ROOT`、`RC_HOST_POLICY_DIR`、`RC_UID`、`RC_GID`、`RC_IPC_ANCHOR_ID`、`RC_RPC_VOLUME`。

本版仅支持本地 Unix Docker endpoint，不支持远端 daemon 路径映射。Docker socket 给予 RC 实际容器管理能力；read-only 挂载标记不能使 Docker API 只读。策略本身不挂载该 socket。

## 启动、切换、恢复

```bash
./scripts/compose.sh up -d --no-build
./scripts/compose.sh attach robot-control
./scripts/compose.sh down
```

基础仅两个容器。anchor 初始化通信卷并永久降权，healthcheck 检查 PID 1 的实际 UID。RC 和策略加入同一个 anchor 的实际 container ID 对应的 IPC namespace。

`startup_policy=null` 时保持空策略。指定默认策略时，RC 先取得部署锁、核对残留容器和外部 reader，再创建默认策略并初始化。成功后暂停。所有冷切换都销毁完整旧组，再启动新组；旧目录被删除不影响按 CID/可信标签清理。

`outputs/runtime` 保存创建意图、CID、代次、退出记录与图像池身份；`outputs/recovery` 保存外部模型 owner/instance/图像 reader。账本是恢复依据，清理完成前不要删除。恢复时先取得本地与全部外部读者退出证明，再在相同 anchor/IPC 代次中核验图像头并回收旧段；IPC 身份不符时保留记录，交给完整 namespace 收尾。Docker create 超时且始终查不到容器时，不能证明请求没有迟到，保留意图并停在 BLOCKED_CLEANUP。恢复 daemon 后重新执行 down；仍无法确认时需要人工核查 daemon 的实际结果，不能靠删除账本绕过。

anchor 被替换或在同一 CID 中重启时，RC 不能原地迁移 IPC namespace；管理器同时核对 CID 与启动代次。它停止旧策略并进入 BLOCKED_INFRA；宿主完成 down 后一起重建 RC 与 anchor。外部读者未释放时 down 会报错并保留基础设施。

RC 的 `timeouts.shutdown_s` 默认 115 秒，基础 stop_grace_period 为 125 秒。退出请求到达时即建立唯一绝对期限，正在执行的 Docker CLI 可取消，失败收尾不重新获得完整预算；需要更长预算时同时调整两者。强制清理即使获得退出证明也保留非零退出事实；宿主 down 会读取 RC 的实际 ExitCode。无法取得证明时 RC 有界退出并保留账本、未知 reader 的图像；down 继续收尾失败则保留 anchor。

## 从旧配置切换

本版不提供 v2 运行兼容。先用旧版本自己的完整部署入口完成旧容器与外部资源收尾，再使用本版。

删除 RC 的 `policy_hosts`、policy 的 `policy_host`/`deployment`，将 schema 改为 3；旧 GPU/cache/model mount 取值写入各策略 `deployment.env`。Qwen `params.model_config_path` 改为相对策略目录的 `model.json`；外部权重仍可共用宿主目录。目录必须符合新 ID 规则，不自动重命名用户目录。根级策略 overlays、`rpc-init` 与 `--no-reset` 均已删除。
