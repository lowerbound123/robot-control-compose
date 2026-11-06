import { readAiResponse, type TranslationProgress } from './ai-stream'
import { buildAgentRequest, type AgentPromptInput } from './agent-prompt'
import { validateDecision } from './agent-capabilities'
import { audit, type AuditSink } from './session-log'
export type { TranslationProgress } from './ai-stream'

export type Effort = 'none' | 'minimal' | 'low' | 'medium' | 'high' | 'xhigh' | 'max'
export type ApiConfig = { llm: boolean; enabled: boolean; model: string; timeout_s: number; effort?: Effort | null }
type Fetcher = (url: string, init: RequestInit) => Promise<Response>

export async function requestDecision(config: ApiConfig, prompt: AgentPromptInput,
  options: { signal?: AbortSignal; fetcher?: Fetcher; onProgress?: (value: TranslationProgress) => void; audit?: AuditSink } = {}) {
  if (!config.llm || !config.enabled || !config.model) throw Error('请先启用并配置 WebUI 服务端 AI 接口，再刷新页面。')
  if (!prompt.input.trim() || prompt.input.length > 4000) throw Error('请输入 1～4000 字的指令。')
  const body = buildAgentRequest(config, prompt)
  const controller = new AbortController()
  let timedOut = false
  const abort = () => controller.abort()
  options.signal?.addEventListener('abort', abort, { once: true })
  if (options.signal?.aborted) abort()
  const timeout = setTimeout(() => { timedOut = true; controller.abort() }, config.timeout_s * 1000)
  try {
    if (controller.signal.aborted) throw new DOMException('Aborted', 'AbortError')
    audit(options.audit, 'model.request', body)
    const response = await (options.fetcher ?? fetch)('/ai/chat/completions', {
      method: 'POST', signal: controller.signal, credentials: 'omit', redirect: 'error', mode: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    })
    audit(options.audit, 'model.response.headers', { status: response.status, contentType: response.headers.get('content-type') })
    if (!response.ok) {
      // The same bounded reader preserves the proxy's error body without treating it as a decision.
      try { await readAiResponse(response, controller.signal, undefined, options.audit) } catch { /* Report HTTP status below. */ }
      throw Error(`AI API 返回 HTTP ${response.status}，本轮未执行任何指令。`)
    }
    const content = await readAiResponse(response, controller.signal, options.onProgress, options.audit)
    if (controller.signal.aborted) throw new DOMException('Aborted', 'AbortError')
    let decision: unknown
    try { decision = JSON.parse(content) } catch { throw Error('AI 返回的工具选择不是有效 JSON，未执行。') }
    audit(options.audit, 'model.decision', decision)
    const validated = validateDecision(decision, prompt.snapshot)
    if (prompt.finalOnly && validated.tool_call) throw Error('本次工具调用达到上限，最后一轮只能总结。')
    return validated
  } catch (error) {
    audit(options.audit, 'model.error', { message: error instanceof Error ? error.message : String(error), timedOut, cancelled: controller.signal.aborted && !timedOut })
    if (timedOut) throw Error('AI 请求超时，本轮未执行任何指令。')
    if (controller.signal.aborted) throw Error('已取消生成，未发送后续指令。')
    if (error instanceof TypeError) throw Error('无法连接同源 AI 代理，请检查 WebUI 服务与服务端 API 配置。')
    throw error
  } finally {
    clearTimeout(timeout)
    options.signal?.removeEventListener('abort', abort)
  }
}

export function readConfig(value: unknown): ApiConfig {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw Error('config.json 必须是 JSON 对象。')
  const config = value as Record<string, unknown>
  if (Object.keys(config).some(key => !['llm', 'enabled', 'model', 'timeout_s', 'effort'].includes(key))
    || (config.effort != null && (typeof config.effort !== 'string' || !['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'].includes(config.effort)))
    || (config.llm !== undefined && typeof config.llm !== 'boolean')
    || typeof config.enabled !== 'boolean' || typeof config.model !== 'string'
    || (config.enabled && !config.model.trim())
    || typeof config.timeout_s !== 'number' || !Number.isFinite(config.timeout_s) || config.timeout_s <= 0 || config.timeout_s > 120) {
    throw Error('AI 公共配置需要 enabled、model、0～120 秒的 timeout_s 以及有效的 effort，请检查 WebUI 服务。')
  }
  return { ...config, llm: config.llm ?? true, effort: config.effort ?? null } as ApiConfig
}
