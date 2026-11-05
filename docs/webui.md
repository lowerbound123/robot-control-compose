# WebUI 三路监控

可选 Vue 静态前端，由独立 Nginx 容器提供。RC 负责三路 MJPEG 和现有 MCP；`llm=true` 时浏览器请求同源 `/ai/chat/completions`，Nginx 使用服务端配置访问 OpenAI 兼容 Chat Completions API；`llm=false` 时按行提交 TUI 文本命令批次。上游不需要支持 CORS。生产环境没有新增 Python/Node 业务后端，不读取 policy 共享内存，也不挂载 Docker socket。

## 准备与启动

先按项目流程准备 RC、anchor 和所需策略镜像。本次动态 MCP 助手需要同步更新 RC 与 WebUI。先用原部署的全部覆盖参数执行统一 down，确认旧运行组清理完成，再按项目流程更新源码／镜像并启动。旧 RC 缺少工具代次保护时，网页会提示升级，不降级为无保护写入。SDK 和 policy 公开契约不变。

```sh
# 只创建缺失的本地配置
test -f webui/config.json || install -m 600 webui/config.json.example webui/config.json
chmod 600 webui/config.json
./scripts/compose.sh -f compose.webui.yaml build webui
./scripts/compose.sh -f compose.webui.yaml up -d

# 保留同一 overlay，走项目统一清理
./scripts/compose.sh -f compose.webui.yaml down
```

默认地址为 `http://127.0.0.1:8080/monitor`；`RC_WEBUI_PORT` 可改宿主端口。基础 compose.yaml 仍只有 RC 与 ipc-anchor，overlay 添加 WebUI。该容器使用宿主 UID/GID、只读根文件系统及临时 `/tmp`。

页面顶部为路由标签、连接状态、暂停／恢复与复位按钮，中间按左腕、头部、右腕横排，底部是自然语言输入。窄窗口时图像换行并在主体区域内滚动，导航与输入框始终留在窗口中。首版只有监控页，尚无可视化策略编排页。

## 与真机、TUI 同时使用

适用于本地 Linux Docker 部署。先准备 RC、anchor、所需策略及 WebUI 镜像；厂商机械臂和相机驱动由现场独立启动，ROS 域、话题和 safety 沿用已验证的真机配置。切换运行方式或修改 RC 监听地址前，先用**原部署的全部覆盖参数**执行 `./scripts/compose.sh ... down`，等待统一清理完成。

真机覆盖文件让 RC 使用宿主机网络。将本机 `configs/rc.acone.yaml` 的 MCP 部分设置为以下值，保留其他字段；缺少文件时先从 `configs/rc.acone.yaml.example` 复制。仓库模板仍默认只监听本地地址。

```yaml
mcp:
  host: 0.0.0.0
  port: 4000
  token_env: ROBOT_CONTROL_MCP_TOKEN
```

此时 `4000` 是宿主机实际监听端口，不使用基础 Compose 的 `RC_MCP_PORT` 端口映射。若配置了 `ROBOT_CONTROL_MCP_TOKEN`，保持 RC 和 WebUI 使用相同值；WebUI 会在代理时添加它。监听全部 IPv4 地址不会自动扩大 RC 的 Host/Origin 白名单，本方案沿用代理的 `Host: localhost:4000`。

WebUI 保留 Docker 内部网络，通过已有 `host-gateway` 映射连接 RC。生产上游由服务端环境变量 `RC_WEBUI_UPSTREAM` 选择：默认 `http://robot-control:4000`，真机使用 `http://host.docker.internal:4000`。生产入口只接受 `http://主机:端口`（DNS 主机名或 IPv4），不接受凭据、尾部斜线、路径、查询参数或 fragment。它独立于下文 AI 的 `endpoint`，不会进入浏览器公共配置。

启动时读取容器 `/etc/hosts` 中 `host.docker.internal` 的 IPv4 映射，生成 Nginx 上游地址；映射缺失或地址非法会阻止启动，诊断不回显配置值。其他服务名继续通过 Docker DNS 动态解析。宿主机映射或环境变量变化后需重新创建 WebUI 容器。

首次使用此功能需重建 WebUI 镜像。在已准备好 `webui/config.json` 的项目根目录中执行，ROS_DOMAIN_ID 使用现场值：

```sh
# 在当前终端保留上游参数；也可以写入本机忽略的 .env
export RC_WEBUI_UPSTREAM=http://host.docker.internal:4000
./scripts/compose.sh -f compose.acone.yaml -f compose.webui.yaml build webui
./scripts/compose.sh -f compose.acone.yaml -f compose.webui.yaml up -d --no-build
./scripts/compose.sh -f compose.acone.yaml -f compose.webui.yaml attach robot-control

# 停止时保留同一组覆盖文件，走项目统一清理
./scripts/compose.sh -f compose.acone.yaml -f compose.webui.yaml down
```

浏览器访问 `http://127.0.0.1:8080/monitor`。WebUI 仍只发布到宿主机本地地址；从其他电脑访问可使用 `ssh -N -L 8080:127.0.0.1:8080 用户@机器人主机`，再打开本机同一网址。TUI 和网页连接同一个 RC，控制权与文本批次打断规则不变。TUI 的 `exit` 或退出快捷键会关闭 RC，网页随后失去后端连接。

现场先检查连接状态及三路画面，再测试需要的控制操作。切回 mock 前先按上述组合 down，随后 `unset RC_WEBUI_UPSTREAM`（若写入 `.env`，同时移除该设置），再按普通 WebUI 流程启动。仅 WebUI 代码变化需要重建 WebUI；未更改 RC 依赖时无需重建 RC 镜像。

## API 配置

编辑忽略的服务端私有文件 `webui/config.json`，兼容旧配置，新增可选 `effort`：

```json
{
  "llm": true,
  "endpoint": "http://host.docker.internal:8000/v1/chat/completions",
  "model": "your-model",
  "api_key": "",
  "timeout_s": 30,
  "effort": null
}
```

`endpoint` 是完整 HTTP(S) 请求地址，不自动追加路径，允许路径前缀和查询参数，不允许用户名、密码、fragment、控制字符或反斜线。非 ASCII 路径/参数使用 URL 百分号编码；Nginx 代理会把其中的字面 `$` 编码为 `%24`，避免变量展开。`localhost` 现在指 **WebUI 容器自身**；宿主服务使用已配置的 `host.docker.internal`，远端服务填写容器可达地址。`timeout_s` 大于 0 且不超过 120，限制每一次模型调用。`effort` 缺省或为 `null` 时不发送推理强度参数；可选 `none`、`minimal`、`low`、`medium`、`high`、`xhigh`、`max`，逐轮映射为 `reasoning_effort`。具体值需由所用模型支持；供应商拒绝时显示错误，不删除参数重试。空 Key 可用于无需鉴权的本地服务，非空 Key 按标准 Bearer token 字符校验。

私有文件只读挂载到 `/run/robot-control-webui/config.json`，位于网页目录之外。固定基础镜像自带的 njs 仅在容器启动时运行一次，校验配置并生成权限为 0600 的 Nginx 代理配置。浏览器读取的 `/config.json` 是另外生成的公共元数据，只有 `llm`、`enabled`、`model`、`timeout_s`、`effort`，不包含真实 endpoint 或 Key。Key 不进入 Git、镜像、前端构建产物或浏览器请求头。

修改私有文件后重新创建 WebUI 容器，再刷新页面；仅修改配置不需要 build：

```sh
./scripts/compose.sh -f compose.webui.yaml up -d --no-deps --force-recreate webui
```

从旧版升级时先 `build webui` 再执行上述命令，让新前端、代理路由和非网页目录挂载一起生效。原文件可以继续使用，但原先指向浏览器本机的 `127.0.0.1` 地址需要按容器网络调整。不要把上游地址填成 WebUI 自己的 `/ai/chat/completions`，这会造成代理循环。

浏览器只向当前页面来源发送 JSON POST，不携带 AI Key；Nginx 只转发到服务端配置的固定上游，独立设置 AI 的 Host 和 Authorization，不接受请求参数改写目标。跨来源请求、非 POST 或非 JSON 请求被拒绝。HTTPS 上游启用 SNI 与系统 CA 证书校验，证书链校验深度放宽到 4 级（Nginx 默认 1 级）；Let's Encrypt Root YE 等新层级会发送 leaf 加多级中间证书，默认深度会把合法链误判为 502。请求不自动重试，上游 3xx 响应按 502 处理。认证、限流及其他接口错误使用本地通用错误内容，保留 HTTP 状态，避免回显凭据或私有地址。

endpoint/model 留空时三路监控和复位仍可用，AI 发送不可用。私有配置缺失或格式非法会阻止 WebUI 启动，诊断不输出配置值。网络、鉴权、超时或响应错误不提交控制操作。

RC 的 `ROBOT_CONTROL_MCP_TOKEN` 独立于 AI Key，由 Nginx 转发时添加，不出现在浏览器配置中。代理接受标准 Bearer token 字符；无效配置会让 WebUI 启动失败且不输出 token。

## 直接命令模式

`llm` 省略时默认为 `true`，保持自然语言流程。需要直接输入 TUI 命令时，私有配置可以只写：

```json
{"llm": false}
```

页面显示“命令输入”，每行一条命令，Enter 发送整批、Shift+Enter 换行。例如：

```text
set example.params.message "人工输入"
policy example example
```

此模式不需要 API 配置，也不调用 LLM；服务端同时关闭 `/ai/chat/completions` 转发。浏览器原文调用 MCP `tui_command`，无需先 take；命令语法、控制权冲突及生命周期行为与本地 TUI 相同。可以输入 set、switch 和已注册的策略命令，策略解析在各行执行时进行。递归 MCP、终端面板和 snapshot 保持不支持。

批次 FIFO 排队，逐行等待完成；某行失败则停止本批剩余行。真实本地 TUI 的非空提交（包括查询）清除当前批次后续行和排队批次，当前已执行行按原规则收尾。“停止”只停止本批后续行，不回滚已完成操作。刷新不取消服务端批次，可用“查询原回执”查看结果，不会重新提交；受理响应丢失时必须先在 RC 核对。

首次启用需升级并按统一清理流程重启 RC，使其提供新增 MCP 工具，然后构建并重新创建 WebUI。旧 RC 不支持时页面会提示升级，不回退执行。后续只修改 `llm` 等 WebUI 配置无需重启 RC。完整批次限制、回执字段见 [MCP 人工文本批次](mcp.md#人工文本批次)。

## 自然语言操作

### 流式输出与状态光效

AI 模式默认发送 `stream: true`，通过同源代理实时接收 SSE。输入框上方的悬浮输出区显示模型实际提供的思考文本和自然语言说明，背景从输入框附近的磨砂逐渐向上透明。接口没有提供思考字段时只显示等待状态，不模拟思考内容。

输出区保留本页最近 50 次请求及每轮思考、说明、工具摘要，刷新清空。模型上下文只带入最近 6 次已结束的对话（包括失败和取消），合计最多 12,000 字符，包含用户输入、最终答复与简短执行事实，不回传思考正文或完整历史日志。历史只用于理解指代，执行依据始终是新查询的状态。默认跟随最新输出，向上滚动后暂停跟随，可点击“回到最新”。原始指令与操作回执仍从详情入口查看。桌面输出区约十行，小屏自动缩短；界面遵循减少动态效果与减少透明度设置。

输入框边缘的状态含义：

- 炫彩旋转：等待 AI、思考或接收流式说明。
- 淡橙呼吸：指令已交给 RC，正在执行或等待操作回执。
- 绿色收缩闪光：整条请求成功完成；中间步骤成功不闪绿。
- 柔和红色：执行或模型响应失败。
- 淡橙静态：等待已经结束，原操作结果仍需确认，此时才显示恢复按钮。

取消且没有待确认操作时，光效淡出；手动解除本地锁定不表示执行成功。`llm=false` 完全隐藏 AI 输出区和炫彩光效，保留玻璃输入框、淡橙等待与绿色成功反馈。

流式文字只用于预览：SSE 必须收到 `[DONE]`，且没有截断、拒答等异常，完整 JSON 通过本轮工具 Schema 和 policy manifest 校验后才会执行。断流、取消、超时和无效输出均不执行部分指令；已受理的 RC 操作仍遵循原回执规则。保留普通 JSON 响应兼容，但不会因为不支持流式而自动重试请求。`timeout_s` 仍限制整次模型请求，默认正文上限 16,000 字符、思考上限 32,000 字符、SSE 累计上限 8MiB。

模型请求和工具等待分别计时：默认最多 8 次工具调用，另留一次只作总结的模型请求；每个已受理操作的等待上限为 120 秒。超时保留原操作 ID 和回执类型，不重新提交。

顶部“复位”左侧提供“暂停／恢复”按钮：运行时显示暂停，暂停时显示恢复，按钮选择不依赖 MCP 是否持有控制权。三个按钮按人工输入处理，分别通过 `tui_command(text="pause"|"resume"|"reset", wait=false)` 提交单条命令，不调用 AI，也不自动先 take，在 `llm=false` 时同样可用。沿用本地 TUI 语义：pause/reset 不额外接管，resume 在 policy 持有控制权时直接恢复，在 MCP 持有控制权时由 RC 按原规则归还并恢复。按钮仍与直接输入和 AI 共用忙碌状态、提交代次及回执保护；执行中、结果待确认或不适用的运行状态下禁用。AI 正在生成时点击按钮会先停止该次生成，再执行明确的按钮操作。

### 动态能力与逐轮执行

每次调用模型前重新获取 MCP 初始化说明、工具目录（description、inputSchema、annotations）以及 `status`、`policies`、`policy_commands`、`help`、`props`。工具目录覆盖配置、策略链、policy 注册命令、重载重试、运动、日志和任务查询；新增注册命令不需要修改前端白名单。`tui_command` 和 `cancel_tui_command` 仅供人工直接模式和工具栏操作，不向 AI 开放。工具调用强制不附带图像；三路监控不受影响。

提示词独立定义角色、执行流程、参数、控制语义、结果和上下文规则。模型每轮输出 `{ "message": "本轮说明", "tool_call": { "name": "工具名", "arguments": {} } }`，只选择一个工具；`tool_call: null` 表示最终答复或询问缺失参数。执行器取得真实结果后才开始下一轮，沿用兼容 Chat Completions 流式 JSON，不要求原生 function calling。

例如“把 example 的消息改成测试”：先读当前配置，明确 `instance_id`，必要时显式 `take`，随后调用 `policy_command` 并核实回执。链内存在 `example-0`、`example-1` 时分别展示和校验，不能把来源名当作明确实例。切换、链应用、重载或重新初始化后重新发现实例目录。获取失败或描述不完整时停止，不猜测工具。

工具参数按实时 Schema 校验；policy 命令另核对 manifest 的参数数量和 JSON 原生类型、目标已加载且可运行。控制代次、图像和等待参数由执行器管理，不接受模型填写。所有工具描述完整保留；请求超过 256KiB 时先删除较早历史，再将大工具结果变成带 `truncated` 标识的节选，并保留操作 ID、状态、目标和错误。仍无法容纳时明确报错。

查询和录制不接管；其他写操作按工具要求明确调用 `take`。复位、切换和生命周期操作完成后保持暂停及 MCP 控制权，只有用户明确要求运行、继续或归还时恢复。停止按钮取消模型请求和后续工具调用，已受理操作仍由 RC 收尾。失败、控制变化、受理未知和等待超时停止自动执行；不会自动重发写操作、冷重试、回滚或恢复旧步骤。

### 两类回执与恢复

普通操作通过 `get_operation` 跟踪。恢复策略默认使用 `return(wait=false)`，只确认归还操作回执，不等待策略自行结束。助手先根据当前实际运行的 policy／原生链成员、当前任务及能力说明决定是否需要业务等待：只有运行组内明确有成员会为本次任务通过 `runtime.finish` 报告结果，且用户要求等待该结果时，才使用 `return_and_wait(timeout_s=0)` 取得任务 ID，再查询 `get_policy_result`。没有这样的成员或说明不足以确认时使用普通 return，并说明仅确认恢复运行。未运行策略的能力、演示命令、handler 时限以及用户说“告诉我结果”，都不能单独作为会回传业务结果的证据。

`return_and_wait(timeout_s=0)` 仍创建业务等待任务，不能作为 no-wait return 的替代。`completed` 才表示策略报告任务完成；`terminated` 显示终止原因并停止后续调用，保持中性光效。普通操作成功只表示 RC 完成操作定义的目标，不额外宣称机械臂实际到位。`exit/quit` 可以提交，但受理后立即结束工具循环；服务断开不是清理成功证明。

待确认操作的 ID、RC 运行标识和回执类型保存在浏览器会话，刷新后可“查询原回执”，旧对话不会继续。原操作确认失败不闪绿；终止结果不显示完成。受理响应丢失、没有 ID 时，应到 RC 核对后解除本地锁定，解锁本身不表示执行成功。

工具栏、直接模式与 AI 共用同一忙碌状态和待确认记录。浏览器附带 RC 的运行标识及控制代次，RC 在受理和排队执行时复核；回执保留完成时的代次，后续操作不能采用人工干预后的新代次继续。所有 MCP 客户端仍共享原有控制权，没有新增多用户仲裁。

## 图像接口与帧率

- `GET /preview/head.mjpg`
- `GET /preview/left_wrist.mjpg`
- `GET /preview/right_wrist.mjpg`
- `GET /preview/status`

目标为每路 640×480、30 Hz、JPEG quality 80；非 4:3 源图完整保留并补边。独立后台编码器读取不可变相机缓存，不依赖 policy 运行。每路最多一个编码任务和一张最新输出，多连接共享结果；慢客户端跳过旧帧，无订阅时停止编码。

状态接口提供源序号、帧龄、尺寸、源/编码/输出帧率和错误。页面“输出 fps”表示 RC 交给流发送路径的有效新帧率，不是浏览器显示帧率。旧图不重复发送凑数；断流超过 2 秒显示过期。页面进入后台时取消图像订阅，回到前台自动重连。

MJPEG 每段包含 `Content-Length`、`X-Sequence`、`X-Received-Monotonic`、`X-Encoded-Monotonic` 供诊断，不修改 JPEG 像素。Nginx 关闭流缓冲、缓存和 gzip。端点沿用 MCP token 保护。

## 开发与验证

```sh
cd webui
npm ci
RC_WEBUI_UPSTREAM=http://127.0.0.1:4000 npm run dev
npm test
npm run build
```

Vite 开发服务使用相同配置校验和公共元数据，通过 Node 自带 fetch 在开发机端代理 AI。开发模式的 endpoint 应对开发机可达；可用 `RC_WEBUI_CONFIG_PATH=/绝对路径/config.json` 指定单独的私有配置。私有文件受 Vite 文件访问拒绝规则保护，`/@fs` 和 raw 导入不能读取该配置。开发模式修改配置后刷新页面即可，生产环境则按上文重新创建 WebUI 容器。

shadcn-vue 组件由官方 CLI 2.9.0 生成并保存在源码中。后续可用 `npx shadcn-vue@2.9.0 add <组件>` 添加，再将默认图标替换为 Font Awesome。生成 CLI 不作为常驻依赖，运行镜像沿用固定 Nginx 镜像自带的 njs，仅启动时生成配置。

激活项目 Python 测试环境后执行 `npm run test:e2e`，自动启动本机 18765/18766 的 mock RC、真实 example worker、明确拒绝 CORS 的兼容 API 替身，以及 5173 的 Vite。测试为 Vite 生成临时私有配置，不使用开发者的真实接口或凭据，不复用未知 Vite 服务。`RC_WEBUI_E2E_URL` 与 `RC_WEBUI_TEST_API` 可指向专用 Docker 夹具；浏览器追踪和截图写入临时目录，可用 `RC_WEBUI_E2E_ARTIFACTS` 指定。测试会修改 mock 状态，不得指向真机。

`RC_WEBUI_TEST_IMAGE=<已构建镜像> python3 -m pytest -q tests/test_webui_proxy_config.py tests/test_webui_network.py` 在实际 njs/Nginx 中验证启动生成、配置权限、URL 转义、非法 JSON 和错误脱敏，并以临时 HTTP 后端验证 bridge/host 两种网络的状态、MCP 请求、三路 multipart 图像转发和认证头。网络用例默认借用已准备的 `robot-control-compose-rc:local` 镜像运行纯 Python 测试后端，可用 `RC_WEBUI_TEST_BACKEND_IMAGE` 指定；不会启动 ROS、policy 或硬件。未指定 WebUI 测试镜像时明确跳过。

`tests/webui_docker_smoke.py` 使用生产 overlay 创建临时项目，支持真实动态 example 容器；SIGTERM 后按项目 down 清理。`webui/tests/preview-benchmark.mjs` 识别该夹具的像素帧序号，统计浏览器实际新帧、延迟和资源趋势，不能直接用于无标记真机图像。

性能验收条件为同机、一个前台浏览器、三路源 30 Hz，连续 10 分钟每路显示新帧均值至少 29.5 fps，同时记录源、编码及输出帧率。Docker、ROS、真实供应商、硬件证据分别记录；mock 通过不代表真机验收。
