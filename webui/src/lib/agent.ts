import { discoverCapabilities, object, prepareToolCall, validateDecision, type AgentGateway, type ToolCall } from './agent-capabilities'
import { MAX_TOOL_CALLS, type AgentExchange, type ConversationTurn } from './agent-prompt'
import { requestDecision, type ApiConfig, type TranslationProgress } from './translator'
import { sameControl, type Payload, type PlanExecutor } from './commands'
import { audit, type AuditSink } from './session-log'

export type AgentRunOptions = {
  config: ApiConfig; input: string; gateway: AgentGateway; executor: PlanExecutor; history: ConversationTurn[];
  signal?: AbortSignal; decide?: typeof requestDecision;
  onRound?: (round: number) => void;
  onPhase?: (phase: 'generating' | 'executing') => void;
  onProgress?: (round: number, value: TranslationProgress) => void;
  onTool?: (round: number, call: ToolCall, result?: Payload) => void;
  emit?: (value: Payload) => void;
  audit?: AuditSink;
}
export type AgentCompletion = { message: string; executed: number; facts: Payload[]; outcome: 'success' | 'neutral' | 'terminated' | 'pending' }

export async function runAgent(options: AgentRunOptions): Promise<AgentCompletion> {
  const { gateway, executor, signal } = options
  const exchanges: AgentExchange[] = [], facts: Payload[] = []
  const check = () => { if (signal?.aborted) throw Error('已取消生成，未发送后续指令。') }
  let expected = await gateway.status()
  try {
    for (let index = 0; index <= MAX_TOOL_CALLS; index++) {
      check()
      const round = index + 1
      options.onRound?.(round)
      const snapshot = await discoverCapabilities(gateway, expected)
      check()
      options.onPhase?.('generating')
      const finalOnly = index === MAX_TOOL_CALLS
      const value = await (options.decide ?? requestDecision)(options.config, {
        input: options.input, snapshot, history: options.history, exchanges, finalOnly,
      }, { signal, audit: (type, data) => audit(options.audit, type, data, { roundId: round }),
        onProgress: value => { if (!signal?.aborted) options.onProgress?.(round, value) } })
      check()
      const decision = validateDecision(value, snapshot)
      if (!sameControl(snapshot.status, await gateway.status())) throw Error('AI 生成期间控制代次变化，本轮未执行。')
      check()
      if (!decision.tool_call) return { message: decision.message, executed: exchanges.length, facts, outcome: exchanges.length ? 'success' : 'neutral' }
      if (finalOnly) throw Error('本次工具调用达到上限，后续操作已停止。')
      const prepared = prepareToolCall(decision.tool_call, snapshot)
      options.onTool?.(round, decision.tool_call)
      options.onPhase?.('executing')
      const result = await executor.runTool(prepared, snapshot.status, options.emit, signal)
      options.onTool?.(round, decision.tool_call, result.payload)
      const fact: Payload = { tool: prepared.name, arguments: decision.tool_call.arguments,
        status: result.payload.status ?? 'succeeded', operation_id: result.payload.operation_id ?? null }
      facts.push(fact)
      if (result.shutdownAccepted) return { message: '退出请求已受理，最终清理结果尚未确认；不会继续调用或自动重发。', executed: exchanges.length + 1, facts, outcome: 'pending' }
      check()
      exchanges.push({ decision, result: result.payload })
      expected = result.control
      if (['return_and_wait', 'get_policy_result'].includes(prepared.name) && object(result.payload.result)
        && result.payload.result.outcome === 'terminated') {
        fact.outcome = 'terminated'
        return { message: `策略任务已终止：${String(result.payload.result.message || '策略报告终止，未报告完成。')}`, executed: exchanges.length, facts, outcome: 'terminated' }
      }
    }
    throw Error('工具调用达到上限，后续操作已停止。')
  } catch (error) {
    const message = error instanceof Error ? error.message : '工具执行失败'
    throw Error(exchanges.length ? `${message} 已确认 ${exchanges.length} 次工具调用，后续已停止。` : message)
  }
}
