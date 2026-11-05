import type { Client } from '@modelcontextprotocol/client'
import { isStatus, ToolRejectedError, type Payload, type RcGateway, type RcStatus } from './commands'
import type { ToolCatalog } from './agent-capabilities'

export class McpGateway implements RcGateway {
  private client?: Client
  private connecting?: Promise<Client>
  private generation = 0
  constructor(private url: URL) {}

  private async connection() {
    if (this.client) return this.client
    if (!this.connecting) {
      const generation = this.generation
      this.connecting = (async () => {
        const { Client, StreamableHTTPClientTransport } = await import('@modelcontextprotocol/client')
        const client = new Client({ name: 'robot-control-webui', version: '0.1.0' })
        try {
          await client.connect(new StreamableHTTPClientTransport(this.url), { prior: { kind: 'legacy' } })
          if (this.generation !== generation) { await client.close(); throw Error('连接已关闭。') }
          this.client = client
          return client
        } catch (error) { await client.close().catch(() => {}); throw error }
      })().finally(() => { this.connecting = undefined })
    }
    return this.connecting
  }

  async call(name: string, args: Payload = {}, expected?: RcStatus, signal?: AbortSignal): Promise<Payload> {
    const client = await this.connection()
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError')
    if (expected && !expected.run_id) throw new ToolRejectedError('RC 缺少运行标识，请同步更新 RC 后再执行控制。')
    try {
      if (name === 'tui_command' || name === 'cancel_tui_command') {
        const catalog = await client.listTools()
        if (!catalog.tools.some(tool => tool.name === name)) {
          throw new ToolRejectedError('当前 RC 不支持文本命令批次，请升级并重启 RC。')
        }
      }
      const response = await client.callTool({ name, arguments: { ...args, ...(expected ? {
        expected_control: { run_id: expected.run_id, control_epoch: expected.control_epoch, generation: expected.generation },
      } : {}) } }, { timeout: 12_000, signal })
      const payload = response.structuredContent
      // A malformed response does not prove that a write was rejected.
      if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw Error('RC 未返回结构化结果，提交是否受理需核对。')
      const result = payload as Payload
      if (response.isError && (typeof result.operation_id !== 'string' || typeof result.status !== 'string')) throw new ToolRejectedError(String(result.error || 'RC 拒绝了请求。'))
      return result
    } catch (error) {
      if (!(error instanceof ToolRejectedError)) {
        if (this.client === client) this.client = undefined
        await client.close().catch(() => {})
      }
      throw error
    }
  }

  async catalog(): Promise<ToolCatalog> {
    const client = await this.connection()
    const catalog = await client.listTools(undefined, { cacheMode: 'refresh' })
    return { instructions: client.getInstructions() || '', tools: catalog.tools }
  }

  async status(): Promise<RcStatus> {
    const value = await this.call('status')
    if (!isStatus(value)) throw Error('RC 状态格式不兼容。')
    return value
  }
  async close() {
    this.generation++
    const client = this.client
    this.client = undefined
    await client?.close()
  }
}
