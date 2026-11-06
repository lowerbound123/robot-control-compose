import type { PreparedTool } from './agent-capabilities'

export type RcStatus = { state: string; control_owner: string; control_epoch: number; generation: number; returning: boolean; run_id?: string; [key: string]: unknown }
export type Payload = Record<string, unknown>
export interface RcGateway {
  status(): Promise<RcStatus>
  call(name: string, args?: Payload, expected?: RcStatus, signal?: AbortSignal): Promise<Payload>
}
export class ToolRejectedError extends Error {}

export function sameControl(a: RcStatus, b: RcStatus) {
  return a.run_id === b.run_id && a.control_epoch === b.control_epoch && a.generation === b.generation
    && a.control_owner === b.control_owner && a.returning === b.returning
}
export function isStatus(value: unknown): value is RcStatus {
  if (!value || typeof value !== 'object') return false
  const v = value as Payload
  return typeof v.control_epoch === 'number' && typeof v.generation === 'number' && typeof v.state === 'string'
    && typeof v.control_owner === 'string' && typeof v.returning === 'boolean'
}
const terminal = new Set(['succeeded', 'failed', 'cancelled'])
const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

export class PlanExecutor {
  busy = false
  unresolvedOperation: string | null = null
  pendingRunId: string | null = null
  pendingKind: 'plan' | 'tui_batch' | 'policy_episode' | null = null
  uncertainSubmission = false
  private cancelled = false
  private detached = false
  private batchCancelSent = false
  private batchCancellation?: Promise<void>
  private batchCancelError = false
  private expected?: RcStatus
  private readonly pollMs: number
  private readonly receiptTimeoutMs: number
  constructor(private gateway: RcGateway, options: { pollMs?: number; receiptTimeoutMs?: number } = {}) {
    this.pollMs = options.pollMs ?? 200
    this.receiptTimeoutMs = options.receiptTimeoutMs ?? 120_000
  }
  cancel() {
    this.cancelled = true
    if (this.pendingKind === 'tui_batch' && this.unresolvedOperation) void this.cancelBatch(this.unresolvedOperation)
  }
  detach() { this.detached = true }

  private cancelBatch(id: string) {
    if (!this.batchCancellation) this.batchCancellation = this.gateway.call('cancel_tui_command', { operation_id: id })
      .then(() => { this.batchCancelSent = true })
      .catch(() => { this.batchCancelError = true })
    return this.batchCancellation
  }

  private async current() {
    if (this.cancelled || this.detached) throw Error('已停止后续步骤；已受理操作仍以 RC 回执为准。')
    const status = await this.gateway.status()
    if (this.cancelled || this.detached) throw Error('已停止后续步骤；已受理操作仍以 RC 回执为准。')
    if (this.expected && !sameControl(this.expected, status)) throw Error('RC 状态或控制代次已变化，可能发生人工干预，后续步骤已停止。')
    return status
  }

  private async receipt(payload: Payload, emit: (value: Payload) => void) {
    const id = payload.operation_id
    if (typeof id !== 'string') throw new ToolRejectedError(String(payload.error || 'RC 未返回操作回执。'))
    this.unresolvedOperation = id
    emit(payload)
    const deadline = Date.now() + this.receiptTimeoutMs
    const query = this.pendingKind === 'policy_episode' ? 'get_policy_result' : 'get_operation'
    while (!terminal.has(String(payload.status))) {
      if (this.detached || (this.cancelled && this.pendingKind !== 'tui_batch') || Date.now() >= deadline) {
        throw Error(`等待已结束，操作 ${id} 尚待确认；不会自动重发。`)
      }
      if (this.cancelled && this.pendingKind === 'tui_batch' && !this.batchCancelSent) {
        await this.cancelBatch(id)
        if (this.batchCancelError) throw Error('停止请求尚未确认，请查询原回执；批次不会重发。')
      }
      await delay(this.pollMs)
      try { payload = await this.gateway.call(query, { operation_id: id }) }
      catch { continue } // Read retries are safe; the accepted write is never repeated.
      if (this.pendingKind === 'tui_batch' && !terminal.has(String(payload.status))) emit(payload)
    }
    this.unresolvedOperation = null
    this.pendingRunId = null
    this.pendingKind = null
    emit(payload)
    if (payload.status !== 'succeeded') throw new ToolRejectedError(String(payload.error || payload.reason || '操作失败或已取消。'))
    return payload
  }

  async runTool(tool: PreparedTool, initial: RcStatus, emit: (value: Payload) => void = () => {}, signal?: AbortSignal) {
    if (this.busy) throw Error('已有指令正在执行。')
    if (!tool.readOnly && (this.unresolvedOperation || this.uncertainSubmission)) throw Error('上一条操作尚待确认，暂不能提交新的控制指令。')
    this.busy = true
    this.cancelled = this.detached = false
    this.expected = initial
    const checkCancelled = () => {
      if (signal?.aborted || this.cancelled || this.detached) throw Error('已停止后续步骤；已受理操作仍以 RC 回执为准。')
    }
    try {
      checkCancelled()
      const before = await this.current()
      checkCancelled()
      if (tool.readOnly) {
        const payload = await this.gateway.call(tool.name, tool.args, undefined, signal)
        emit({ tool: tool.name, arguments: tool.args, result: payload })
        if (['get_operation', 'get_policy_result'].includes(tool.name) && payload.operation_id === this.unresolvedOperation
          && terminal.has(String(payload.status))) {
          this.unresolvedOperation = this.pendingRunId = this.pendingKind = null
        }
        checkCancelled()
        await this.current()
        return { payload, control: before, shutdownAccepted: false }
      }
      this.pendingRunId = before.run_id ?? null
      this.pendingKind = tool.episode ? 'policy_episode' : 'plan'
      this.uncertainSubmission = true
      emit({ tool: tool.name, arguments: tool.args, submission: 'pending' })
      let accepted: Payload
      try { accepted = await this.gateway.call(tool.name, tool.args, before, signal) }
      catch (error) {
        if (error instanceof ToolRejectedError) {
          this.uncertainSubmission = false
          this.pendingRunId = this.pendingKind = null
        }
        throw error
      }
      if (typeof accepted.operation_id !== 'string' || typeof accepted.status !== 'string') throw Error('提交响应缺少操作回执，请在 RC 核对；不会自动重发。')
      this.uncertainSubmission = false
      if (tool.shutdown) {
        this.unresolvedOperation = accepted.operation_id
        emit(accepted)
        return { payload: accepted, control: before, shutdownAccepted: true }
      }
      const completed = await this.receipt(accepted, emit)
      const live = await this.gateway.status()
      const proof = completed.completion_control ?? (isStatus(completed.result) ? completed.result : undefined)
      if (!proof || typeof proof !== 'object' || Array.isArray(proof)) throw Error('RC 回执缺少完成代次，请同步升级 RC；后续工具已停止。')
      const guard = proof as Payload
      if (!['run_id', 'control_epoch', 'generation'].every(key => guard[key] === live[key])) throw Error('操作完成后控制代次已变化，后续工具已停止。')
      if (tool.name === 'record' && !sameControl(before, live)) throw Error('录制期间控制状态变化，后续工具已停止。')
      this.expected = live
      checkCancelled()
      return { payload: completed, control: live, shutdownAccepted: false }
    } finally { this.busy = false }
  }

  async runText(text: string, initial: RcStatus, emit: (value: Payload) => void = () => {}) {
    if (!text.trim() || text.length > 4000) throw Error('请输入 1～4000 字的命令，每行一条。')
    if (this.busy) throw Error('已有指令正在执行。')
    if (this.unresolvedOperation || this.uncertainSubmission) throw Error('上一条操作尚待确认，暂不能提交新批次。')
    this.busy = true
    this.cancelled = this.detached = this.batchCancelSent = false
    this.batchCancellation = undefined
    this.batchCancelError = false
    this.expected = initial
    try {
      const before = await this.current()
      this.pendingRunId = before.run_id ?? null
      this.pendingKind = 'tui_batch'
      // Persist before the write: refreshing before its response must not lose
      // the fact that the server may already have accepted the batch.
      this.uncertainSubmission = true
      emit({ submission: 'pending', kind: 'tui_batch' })
      let accepted: Payload
      try {
        accepted = await this.gateway.call('tui_command', { text, wait: false }, before)
        if (typeof accepted.operation_id !== 'string') throw Error('批次受理结果缺少操作 ID，请在 RC 核对。')
      } catch (error) {
        if (error instanceof ToolRejectedError) {
          this.uncertainSubmission = false
          this.pendingRunId = this.pendingKind = null
        }
        throw error
      }
      this.uncertainSubmission = false
      return await this.receipt(accepted, emit)
    } finally { this.busy = false }
  }

  async reconcile() {
    if (this.uncertainSubmission) throw Error('提交响应已丢失，是否受理未知。请在 RC 核对操作后解除本地锁定。')
    if (!this.unresolvedOperation) return null
    const result = await this.gateway.call(this.pendingKind === 'policy_episode' ? 'get_policy_result' : 'get_operation', { operation_id: this.unresolvedOperation })
    if (terminal.has(String(result.status))) { this.unresolvedOperation = null; this.pendingRunId = null; this.pendingKind = null }
    return result
  }

  acknowledgeUnknown() {
    if (this.busy) throw Error('请先等待当前操作结束。')
    this.uncertainSubmission = false
    if (!this.unresolvedOperation) { this.pendingRunId = null; this.pendingKind = null }
    if (!this.unresolvedOperation) this.pendingRunId = null
    // An operation with a known ID must still be reconciled, never forgotten.
  }
}
