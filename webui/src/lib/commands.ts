import Ajv from 'ajv'

export type RcStatus = { state: string; control_owner: string; control_epoch: number; generation: number; returning: boolean; run_id?: string; [key: string]: unknown }
export type Step = { action: 'status' | 'state' | 'policies' | 'record_status' | 'take' | 'return' | 'pause' | 'resume' | 'reset' }
  | { action: 'switch'; policy: string }
  | { action: 'record'; mode: 'on' | 'off' | 'toggle' }
export type CommandPlan = { message: string; steps: Step[] }
export type Payload = Record<string, unknown>
export interface RcGateway {
  status(): Promise<RcStatus>
  call(name: string, args?: Payload, expected?: RcStatus): Promise<Payload>
}
export class ToolRejectedError extends Error {}

const simple = ['status', 'state', 'policies', 'record_status', 'take', 'return', 'pause', 'resume', 'reset']
export const planSchema = {
  type: 'object', additionalProperties: false, required: ['message', 'steps'],
  properties: {
    message: { type: 'string', minLength: 1, maxLength: 1000 },
    steps: { type: 'array', maxItems: 12, items: { oneOf: [
      { type: 'object', additionalProperties: false, required: ['action'], properties: { action: { enum: simple } } },
      { type: 'object', additionalProperties: false, required: ['action', 'policy'], properties: { action: { const: 'switch' }, policy: { type: 'string', minLength: 1, maxLength: 64 } } },
      { type: 'object', additionalProperties: false, required: ['action', 'mode'], properties: { action: { const: 'record' }, mode: { enum: ['on', 'off', 'toggle'] } } },
    ] } },
  },
}
const check = new Ajv({ allErrors: true, coerceTypes: false, removeAdditional: false }).compile(planSchema)
export function validatePlan(value: unknown, policies: string[]): CommandPlan {
  if (!check(value)) throw Error('AI 返回的指令格式或操作不在允许范围内，未执行。')
  const plan = value as CommandPlan
  if (plan.steps.some(step => step.action === 'switch' && !policies.includes(step.policy))) {
    throw Error('AI 选择了当前不可用的策略，整份清单未执行。')
  }
  return plan
}

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
export const actionLabels: Record<Step['action'], string> = {
  status: '查询状态', state: '读取机器人反馈', policies: '列出策略', record_status: '查询录制',
  take: '接管控制', return: '归还控制', pause: '暂停', resume: '继续', reset: '复位', switch: '切换策略', record: '录制',
}
const readOnly = new Set(['status', 'state', 'policies', 'record_status'])
const terminal = new Set(['succeeded', 'failed', 'cancelled'])
const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

export class PlanExecutor {
  busy = false
  unresolvedOperation: string | null = null
  pendingRunId: string | null = null
  uncertainSubmission = false
  private cancelled = false
  private expected?: RcStatus
  private readonly pollMs: number
  private readonly receiptTimeoutMs: number
  constructor(private gateway: RcGateway, options: { pollMs?: number; receiptTimeoutMs?: number } = {}) {
    this.pollMs = options.pollMs ?? 200
    this.receiptTimeoutMs = options.receiptTimeoutMs ?? 120_000
  }
  cancel() { this.cancelled = true }

  private async current() {
    if (this.cancelled) throw Error('已停止后续步骤；已受理操作仍以 RC 回执为准。')
    const status = await this.gateway.status()
    if (this.expected && !sameControl(this.expected, status)) throw Error('RC 状态或控制代次已变化，可能发生人工干预，后续步骤已停止。')
    return status
  }

  private async receipt(payload: Payload, emit: (value: Payload) => void) {
    const id = payload.operation_id
    if (typeof id !== 'string') throw new ToolRejectedError(String(payload.error || 'RC 未返回操作回执。'))
    this.unresolvedOperation = id
    emit(payload)
    const deadline = Date.now() + this.receiptTimeoutMs
    while (!terminal.has(String(payload.status))) {
      if (this.cancelled || Date.now() >= deadline) throw Error(`等待已结束，操作 ${id} 尚待确认；不会自动重发。`)
      await delay(this.pollMs)
      try { payload = await this.gateway.call('get_operation', { operation_id: id }) }
      catch { continue } // Read retries are safe; the accepted write is never repeated.
    }
    this.unresolvedOperation = null
    this.pendingRunId = null
    emit(payload)
    if (payload.status !== 'succeeded') throw new ToolRejectedError(String(payload.error || payload.reason || '操作失败或已取消。'))
    return payload
  }

  private async write(name: string, args: Payload, emit: (value: Payload) => void, changesControl = true) {
    const before = await this.current()
    this.pendingRunId = before.run_id ?? null
    let accepted: Payload
    try { accepted = await this.gateway.call(name, { ...args, wait: false }, before) }
    catch (error) {
      if (!(error instanceof ToolRejectedError)) this.uncertainSubmission = true
      else this.pendingRunId = null
      throw error
    }
    const completed = await this.receipt(accepted, emit)
    if (changesControl) {
      // Completion snapshots belong to this operation. A later status read
      // must not silently adopt a human's newer generation into our plan.
      const snapshot = isStatus(completed.result) ? completed.result : completed.controller
      if (!isStatus(snapshot)) throw Error('RC 回执缺少控制状态，后续步骤已停止。')
      this.expected = snapshot
    } else this.expected = before
    await this.current()
  }

  async run(plan: CommandPlan, initial: RcStatus, emit: (value: Payload) => void = () => {}) {
    if (this.busy) throw Error('已有指令正在执行。')
    if ((this.unresolvedOperation || this.uncertainSubmission) && plan.steps.some(step => !readOnly.has(step.action))) {
      throw Error('上一条操作尚待确认，暂不能提交新的控制指令。')
    }
    this.busy = true
    this.cancelled = false
    this.expected = initial
    try {
      for (const [index, step] of plan.steps.entries()) {
        const status = await this.current()
        emit({ step: index + 1, total: plan.steps.length, action: step.action, label: actionLabels[step.action] })
        if (readOnly.has(step.action)) {
          const result = await this.gateway.call(step.action === 'record_status' ? 'record' : step.action,
            step.action === 'record_status' ? { action: 'status' } : {})
          emit({ action: step.action, result })
        } else if (step.action === 'record') {
          await this.write('record', { action: step.mode }, emit, false)
        } else if (step.action === 'return') {
          await this.write('return', {}, emit)
        } else if (step.action === 'take') {
          await this.write('take', {}, emit)
        } else {
          if (step.action === 'resume' && status.state === 'running' && status.control_owner === 'policy') continue
          if (status.control_owner !== 'mcp') await this.write('take', {}, emit)
          if (step.action === 'resume') await this.write('return', {}, emit)
          else await this.write('command', { line: step.action === 'switch' ? `switch ${step.policy}` : step.action }, emit)
        }
      }
    } finally { this.busy = false }
  }

  async reconcile() {
    if (this.uncertainSubmission) throw Error('提交响应已丢失，是否受理未知。请在 RC 核对操作后解除本地锁定。')
    if (!this.unresolvedOperation) return null
    const result = await this.gateway.call('get_operation', { operation_id: this.unresolvedOperation })
    if (terminal.has(String(result.status))) { this.unresolvedOperation = null; this.pendingRunId = null }
    return result
  }

  acknowledgeUnknown() {
    if (this.busy) throw Error('请先等待当前操作结束。')
    this.uncertainSubmission = false
    if (!this.unresolvedOperation) this.pendingRunId = null
    // An operation with a known ID must still be reconciled, never forgotten.
  }
}
