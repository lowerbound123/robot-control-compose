# WebUI 三路监控

可选 Vue 静态前端，由独立 Nginx 容器提供。RC 负责三路 MJPEG 和现有 MCP；浏览器直接访问配置的 OpenAI 兼容 Chat Completions API。没有新增 Python/Node 业务后端，不读取 policy 共享内存，也不挂载 Docker socket。

## 准备与启动

先按项目流程准备 RC、anchor 和所需策略镜像。若 RC 已运行，更新源码后先用项目 down 入口完成动态策略收尾，再启动新版 RC，使预览与控制代次校验生效。

```sh
# 只创建缺失的本地配置
test -f webui/config.json || cp webui/config.json.example webui/config.json
./scripts/compose.sh -f compose.webui.yaml build webui
./scripts/compose.sh -f compose.webui.yaml up -d

# 保留同一 overlay，走项目统一清理
./scripts/compose.sh -f compose.webui.yaml down
```

默认地址为 `http://127.0.0.1:8080/monitor`；`RC_WEBUI_PORT` 可改宿主端口。基础 compose.yaml 仍只有 RC 与 ipc-anchor，overlay 添加 WebUI。该容器使用宿主 UID/GID、只读根文件系统及临时 `/tmp`。

页面顶部为路由标签、连接状态与复位按钮，中间按左腕、头部、右腕横排，底部是自然语言输入。窄窗口时图像换行并在主体区域内滚动，导航与输入框始终留在窗口中。首版只有监控页，尚无可视化策略编排页。

## API 配置

编辑忽略的 `webui/config.json`：

```json
{
  "endpoint": "http://127.0.0.1:8000/v1/chat/completions",
  "model": "your-model",
  "api_key": "",
  "timeout_s": 30
}
```

`endpoint` 是完整请求地址，不自动追加路径；`localhost` 指浏览器所在主机。`timeout_s` 大于 0 且不超过 120。修改后刷新页面，无需重新构建；若编辑器原子替换文件使 Docker 文件挂载仍指向旧 inode，重新创建 WebUI 容器。

配置经只读挂载提供并由浏览器读取，因此 API Key 在客户端可见，这是浏览器直连方案的预期边界；它不进入 Git、镜像、构建产物或应用日志。兼容 API 必须允许页面来源的 CORS 请求及 `Content-Type`／`Authorization` 头。空 Key 可用于无需鉴权的本地服务。

API 未配置时三路监控和复位可用，AI 发送不可用。网络、跨域、鉴权或响应错误显示原因，不提交控制操作。

RC 的 `ROBOT_CONTROL_MCP_TOKEN` 独立于 AI Key，由 Nginx 转发时添加，不出现在浏览器配置中。代理接受标准 Bearer token 字符；无效配置会让 WebUI 启动失败且不输出 token。

## 自然语言操作

模型只收到允许的操作说明、当前控制状态、可用策略名称和用户文本，不接收图像。首版支持：

- 状态、机器人反馈、策略列表、录制状态查询。
- 接管、归还、暂停、继续、复位。
- 切换到已发现策略，例如“切换到 example_chain，然后继续”。
- 开始、停止、切换录制。

整份 JSON 清单通过白名单与参数校验后才开始执行。手动运动、配置修改、退出、链编辑及“抓取杯子”等新模型业务任务不在首版能力内；含糊或超范围请求返回说明，不能只执行其中一部分。

需要控制权时先 take；录制沿用无需接管的例外。复位和切换完成后保持暂停及 MCP 所有权，只有明确继续/归还才恢复 policy。复位按钮直接调用同一执行器，不经过 AI。回执成功表示 RC 已完成操作定义的目标发送，不额外宣称机械臂实际到位。

每步保存操作 ID 并查询终态。断连只重试查询，不自动重发写操作；失败或取消停止后续步骤。停止按钮结束翻译或后续步骤，已受理操作仍由 RC 管理。待确认操作保存在浏览器会话，刷新后可查询原回执；若提交响应丢失、没有操作 ID，应到 RC 核对后明确解除本地锁定。旧清单不会继续或重发。

浏览器附带 RC 的运行标识及控制代次，RC 在受理和执行时复核，拒绝人工干预或重启前形成的旧指令。所有 MCP 客户端仍共享原有控制权，首版没有新增多用户仲裁。

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

shadcn-vue 组件由官方 CLI 2.9.0 生成并保存在源码中。后续可用 `npx shadcn-vue@2.9.0 add <组件>` 添加，再将默认图标替换为 Font Awesome。生成 CLI 不作为常驻依赖，运行镜像只包含 Nginx 和静态产物。

激活项目 Python 测试环境后执行 `npm run test:e2e`，自动启动本机 18765/18766 的 mock RC、真实 example worker、本地兼容 API 替身，以及 5173 的 Vite；已启动时复用。`RC_WEBUI_E2E_URL` 与 `RC_WEBUI_TEST_API` 可指向专用 Docker 夹具。测试会修改 mock 状态，不得指向真机。

`tests/webui_docker_smoke.py` 使用生产 overlay 创建临时项目，支持真实动态 example 容器；SIGTERM 后按项目 down 清理。`webui/tests/preview-benchmark.mjs` 识别该夹具的像素帧序号，统计浏览器实际新帧、延迟和资源趋势，不能直接用于无标记真机图像。

性能验收条件为同机、一个前台浏览器、三路源 30 Hz，连续 10 分钟每路显示新帧均值至少 29.5 fps，同时记录源、编码及输出帧率。Docker、ROS、真实供应商、硬件证据分别记录；mock 通过不代表真机验收。
