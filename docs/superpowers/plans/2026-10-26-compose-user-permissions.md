# Compose 普通用户运行与持久化权限修复

Goal: 新建 snapshot、保存配置及 outputs 归运行部署命令的宿主用户所有，宿主可直接阅读和编辑。

Spec: 用户已批准长期修复；现有测试文件保持原样，不执行迁移、chmod 或 chown。

Architecture: 所有 RC/policy/ROS 业务进程统一使用必填的 RC_UID/RC_GID。普通用户运行 setup-user.sh 写入本机 .env 并创建默认挂载目录。一次性 rpc-init 只初始化 Docker 内部 RPC 卷，业务服务等待其成功。每个业务容器有该用户拥有的临时 HOME；HF 缓存继续使用宿主挂载。禁止 Docker 自动创建缺失的宿主 bind 目录。

Tech stack: POSIX shell、Docker Compose >= 2.24.4、现有 Python 3.10/3.11/3.14 镜像、pytest。

## Global constraints

- 只改 Compose 项目；保留已有 Word 文档。现有快照、配置和缓存数据不做权限迁移。
- socket 和共享内存的私有权限不放宽；业务进程不使用 root。仅内部卷初始化和测试 fixture 准备允许 root。
- 不修改 SDK、控制线程、图像租约和生命周期。测试使用唯一项目与临时目录，结束清理这些测试资源。
- 按 AGENTS.md 在 .codex/task-state.md 维护唯一账本；用户要求直接实现，在当前干净 feature checkout 工作。

## Task 1: 回归证据与宿主初始化

先为 setup-user.sh 编写真实文件行为测试：保留既有 .env 的其他值、不执行其内容、正确写入 UID/GID、重复执行幂等、不改变既有数据权限。为 Compose 实际 config 输出增加用户和依赖约束检查。扩展实际 smoke，验证运行 UID/GID、可写 HOME、快照/配置/outputs 的所有权和私有权限。

Expected: 新测试在旧配置下失败（缺少脚本/运行身份为 root）。实现 setup-user.sh 和 .env.example 后相应测试通过。

## Task 2: 全部服务与测试拓扑

修改基础、LLM、Qwen、ROS 及测试 overlay 的身份、HOME、初始化依赖和 bind 配置。RPC 初始化兼容先前遗留的内部 lock 文件，不挂载用户目录。测试 fixture 生成后仅调整其临时命名卷的所有者。镜像构建时确保复制进去的源码与配置可由任意运行 UID 读取，不能依赖宿主源文件原本具有全局读取权限。

Expected: 所有生产/测试 Compose 组合 config 通过；缺 UID/GID 明确失败；独立 Docker 项目验证 private socket、共享内存、快照/配置和 ROS 通信通过。Qwen 以现有 CPU 替身测试真实 3.11 容器通信，不声称 GPU/真机验收。

## Task 3: 文档、完整回归与审查

更新快速启动、Ubuntu、Qwen、LLM、扩展 policy 教程；LLM 合并 .env 与 .env.llm。说明首次升级应停止后重建容器，不能只 restart。运行完整套件及真实容器 smoke，完成一次独立最终审查并处理实际问题。

Expected: 完整回归通过，独立测试资源清理，新旧用户数据权限没有被修复脚本更改。

## Review focus

- 新 clone 缺少目录、已有 .env/密钥/特殊字符、从其他 cwd 调用脚本。
- UID/GID 缺失、root、非默认值；各 overlay 的 user/environment/depends_on 合并。
- 旧 RPC lock、原子保存后的 0600 配置、0700 快照目录及多 Python 进程共享图像。
- Qwen 自定义模型/缓存目录、LLM --env-file 对 .env 的覆盖、只读 policy 挂载。
- 测试 fixture 命名卷与服务 HOME 的可写性，禁止改动历史用户数据。
