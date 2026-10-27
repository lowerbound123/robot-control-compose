# Schema 3 部署准备

## 配置和镜像

```bash
./setup.sh --policies hold
# 或准备任意合规范目录；省略 --policies 时准备全部有效策略
./setup.sh --policies my_policy,qwen35_twostage
./setup.sh --config-only
```

setup 使用系统 Python 标准库和 Docker CLI，无 Conda。正式 `policy.json` 是发现依据，`.example` 只是补齐缺失配置的模板；只有模板时目录显示 needs_config。无效目录会显示原因。通用校验不解释 provider、model.json、GPU 或 HF 业务字段，真正业务校验由目标策略执行。

setup 先合并、验证，再以原子写入更新本机文件。既有值保留，发生变更的文件备份到 `.setup/backups/`，权限为 0600。配置、密钥、输出与运行账本都被 Git 和镜像构建上下文忽略。

每个策略的 `compose.yaml` 只能有一个 `policy` 服务，必须显式指定 image。setup 对 build 声明构建镜像，直接复用已准备好的本地镜像，仅对缺少的预构建镜像执行 pull。相同 image tag 只有在 Dockerfile 内容、context、args、target 与其他有效输入一致时才能去重；不同输入报错。两个 Qwen 各有完整 build 声明与依赖锁，通过内容相同的私有依赖上下文共享镜像；单独选择 twostage 不依赖 single 的文件。

运行期间只使用准备好的本地 image ID。模型权重与外部 mount 目录应提前准备，setup 不下载权重、不访问供应商、不验证真实推理。

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
