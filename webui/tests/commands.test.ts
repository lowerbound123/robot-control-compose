import { describe, expect, it } from 'vitest'
import { validatePlan, PlanExecutor, type RcGateway } from '../src/lib/commands'

function gateway() {
  const calls: Array<[string, Record<string, unknown>]> = []
  let state = { run_id: 'test-run', state: 'paused', control_owner: 'policy', control_epoch: 1, generation: 1, returning: false }
  let failNext = false
  let disconnected = false
  const api: RcGateway = {
    async status() { if (disconnected) throw Error('offline'); return { ...state } },
    async call(name, args = {}) {
      calls.push([name, args])
      if (name === 'get_operation') {
        if (disconnected) throw Error('offline')
        return { operation_id: 'op', status: failNext ? 'failed' : 'succeeded', error: failNext ? 'failed step' : undefined, result: { ...state }, controller: { ...state } }
      }
      if (name === 'status') return { ...state }
      if (name === 'take') state = { ...state, control_owner: 'mcp', control_epoch: state.control_epoch + 1, generation: state.generation + 1 }
      if (name === 'return') state = { ...state, control_owner: 'policy', state: 'running', control_epoch: state.control_epoch + 1, generation: state.generation + 1 }
      if (name === 'command') state = { ...state, state: 'paused', generation: state.generation + 1 }
      return { operation_id: 'op', status: 'queued', controller: { ...state } }
    },
  }
  return { api, calls, intervene: () => { state.generation++ }, fail: () => { failNext = true }, offline: (value: boolean) => { disconnected = value } }
}

describe('translation contract', () => {
  it('rejects the entire plan before reset if a later action or policy is invalid', () => {
    expect(() => validatePlan({ message: 'x', steps: [{ action: 'reset' }, { action: 'quit' }] }, ['example'])).toThrow()
    expect(() => validatePlan({ message: 'x', steps: [{ action: 'reset' }, { action: 'switch', policy: 'missing' }] }, ['example'])).toThrow()
  })
  it('rejects free commands, extra fields and malformed arguments without coercion', () => {
    for (const step of [{ action: 'command', line: 'exit' }, { action: 'reset', line: 'exit' }, { action: 'record', mode: true }]) {
      expect(() => validatePlan({ message: 'x', steps: [step] }, ['example'])).toThrow()
    }
  })
  it('accepts an explanation without executable steps', () => {
    expect(validatePlan({ message: '请指定策略', steps: [] }, []).steps).toEqual([])
  })
})

describe('receipt-driven execution', () => {
  it('takes before reset, waits for its receipt and stays paused without returning', async () => {
    const g = gateway(), executor = new PlanExecutor(g.api, { pollMs: 1 })
    await executor.run(validatePlan({ message: '复位', steps: [{ action: 'reset' }] }, []), await g.api.status())
    expect(g.calls.filter(([name]) => name !== 'get_operation')).toEqual([['take', { wait: false }], ['command', { line: 'reset', wait: false }]])
    expect(g.calls.filter(([name]) => name === 'get_operation')).toHaveLength(2)
  })
  it('recording changes never take or return control', async () => {
    const g = gateway(), executor = new PlanExecutor(g.api, { pollMs: 1 })
    await executor.run(validatePlan({ message: '录制', steps: [{ action: 'record', mode: 'on' }] }, []), await g.api.status())
    expect(g.calls[0]).toEqual(['record', { action: 'on', wait: false }])
    expect(g.calls.some(([name]) => name === 'take' || name === 'return')).toBe(false)
  })
  it('does not submit a translated plan after manual intervention', async () => {
    const g = gateway(), executor = new PlanExecutor(g.api)
    const original = await g.api.status()
    g.intervene()
    await expect(executor.run(validatePlan({ message: '复位', steps: [{ action: 'reset' }] }, []), original)).rejects.toThrow(/状态|代次|干预/)
    expect(g.calls).toHaveLength(0)
  })
  it('stops following actions on a failed receipt', async () => {
    const g = gateway(), executor = new PlanExecutor(g.api, { pollMs: 1 })
    g.fail()
    await expect(executor.run(validatePlan({ message: '复位再继续', steps: [{ action: 'reset' }, { action: 'resume' }] }, []), await g.api.status())).rejects.toThrow()
    expect(g.calls.some(([name]) => name === 'command' || name === 'return')).toBe(false)
  })
  it('blocks concurrent plans and never repeats an accepted write after disconnect', async () => {
    const g = gateway(), executor = new PlanExecutor(g.api, { pollMs: 1, receiptTimeoutMs: 30 })
    const original = g.api.call.bind(g.api)
    g.api.call = async (name, args) => { const r = await original(name, args); if (name === 'take') g.offline(true); return r }
    const state = await g.api.status(), plan = validatePlan({ message: '复位', steps: [{ action: 'reset' }] }, [])
    const running = executor.run(plan, state)
    await expect(executor.run(plan, state)).rejects.toThrow(/正在|执行/)
    await expect(running).rejects.toThrow()
    expect(g.calls.filter(([name]) => name === 'take')).toHaveLength(1)
    expect(executor.unresolvedOperation).toBe('op')
    expect(executor.pendingRunId).toBe('test-run')
    g.offline(false)
    await executor.reconcile()
    expect(executor.unresolvedOperation).toBeNull()
    expect(executor.pendingRunId).toBeNull()
    expect(g.calls.some(([name]) => name === 'command')).toBe(false)
  })
})
