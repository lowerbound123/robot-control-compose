import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { CommandPlan, Payload, RcStatus } from '../src/lib/commands'
import { useControlStore } from '../src/stores/control'

const mock = vi.hoisted(() => ({ status: vi.fn(), call: vi.fn(), close: vi.fn(async () => {}), translate: vi.fn() }))
vi.mock('../src/lib/mcp', () => ({ McpGateway: class { status = mock.status; call = mock.call; close = mock.close } }))
// Presentation tests use a controllable model/executor boundary; actual dynamic
// decisions and discovery are covered by agent-loop tests and the HTTP browser fixture.
vi.mock('../src/lib/agent', () => ({ runAgent: async (options: any) => {
  options.onRound(1)
  options.onPhase('generating')
  const plan = await mock.translate(options.config, options.input, {}, [], options.signal, undefined,
    (value: object) => options.onProgress(1, value))
  if (options.signal.aborted) throw Error('已取消生成')
  options.onPhase('executing')
  await options.executor.run(plan, await options.gateway.status(), options.emit)
  return { message: plan.message, executed: plan.steps.length, facts: [], outcome: plan.steps.length ? 'success' : 'neutral' }
} }))
let state: RcStatus, store: ReturnType<typeof useControlStore>, pending: Map<string, boolean>, id: number, direct: boolean
const plan = { message: '准备复位', steps: [{ action: 'reset' }] } satisfies CommandPlan

beforeEach(() => {
  vi.useFakeTimers()
  vi.clearAllMocks()
  setActivePinia(createPinia())
  state = { run_id: 'run', state: 'paused', control_owner: 'policy', control_epoch: 1, generation: 1, returning: false }
  pending = new Map(); id = 0; direct = false
  vi.stubGlobal('window', { location: { origin: 'http://localhost' } })
  vi.stubGlobal('document', { hidden: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })
  const saved = new Map()
  vi.stubGlobal('sessionStorage', { getItem: (key: string) => saved.get(key) || null, setItem: (key: string, value: string) => saved.set(key, value), removeItem: (key: string) => saved.delete(key) })
  vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(JSON.stringify(url === '/config.json'
    ? { llm: !direct, enabled: !direct, model: direct ? '' : 'test', timeout_s: 3 }
    : url === '/health' ? state : { cameras: {} }))))
  mock.status.mockImplementation(async () => ({ ...state }))
  mock.call.mockImplementation(async (name: string, args?: Payload) => {
    if (name === 'policies') return { result: [{ valid: true, id: 'example' }] }
    if (name === 'status') return { ...state }
    if (name === 'get_operation') return { operation_id: args!.operation_id, status: pending.get(String(args!.operation_id)) ? 'succeeded' : 'running', result: { ...state } }
    if (name === 'take') state = { ...state, control_owner: 'mcp', control_epoch: state.control_epoch + 1, generation: state.generation + 1 }
    if (name === 'command') state = { ...state, state: 'paused', generation: state.generation + 1 }
    if (name === 'return') state = { ...state, state: 'running', control_owner: 'policy', control_epoch: state.control_epoch + 1, generation: state.generation + 1 }
    const operation_id = `op-${++id}`
    pending.set(operation_id, false)
    return { operation_id, status: 'queued' }
  })
  mock.translate.mockResolvedValue(plan)
  store = useControlStore()
})
afterEach(() => { store.stop(); vi.useRealTimers(); vi.unstubAllGlobals() })
async function flush() { await vi.advanceTimersByTimeAsync(0) }

it('shows real streamed text, stays orange across intermediate receipts and pulses only after all steps', async () => {
  await store.start()
  let finish!: (plan: CommandPlan) => void
  mock.translate.mockImplementation((...args) => {
    args[6]({ kind: 'reasoning', text: '检查控制状态' })
    args[6]({ kind: 'message', text: '准备' })
    return new Promise(resolve => { finish = resolve })
  })
  const running = store.submit('复位')
  await flush()
  expect(store.phase).toBe('streaming')
  expect(store.history[0]).toMatchObject({ input: '复位', reasoning: '检查控制状态', message: '准备' })
  expect(mock.call.mock.calls.some(([name]) => name === 'take')).toBe(false)
  finish(plan); await flush()
  expect(store.phase).toBe('executing')
  expect(store.unresolved).toBe('op-1')
  expect(store.showRecovery).toBe(false)
  pending.set('op-1', true); await vi.advanceTimersByTimeAsync(200)
  expect(store.phase).toBe('executing')
  expect(store.unresolved).toBe('op-2')
  pending.set('op-2', true); await vi.advanceTimersByTimeAsync(200)
  await running
  expect(store.phase).toBe('success')
  expect(store.history[0].outcome).toBe('success')
  await vi.advanceTimersByTimeAsync(650)
  expect(store.phase).toBe('idle')
})

it('keeps a cancelled accepted operation pending until its receipt is reconciled', async () => {
  await store.start()
  const running = store.submit('复位'); await flush()
  store.cancel(); await vi.advanceTimersByTimeAsync(200); await running
  expect(store.phase).toBe('pending')
  expect(store.showRecovery).toBe(true)
  pending.set('op-1', true)
  await store.reconcile()
  expect(store.phase).toBe('success')
  expect(store.showRecovery).toBe(false)
  expect(mock.call.mock.calls.some(([name]) => name === 'command')).toBe(false)
})

it('ignores late model text and a late complete plan after cancellation', async () => {
  await store.start()
  let finish!: (value: CommandPlan) => void, progress!: (value: object) => void
  mock.translate.mockImplementation((...args) => { progress = args[6]; return new Promise(resolve => { finish = resolve }) })
  const running = store.submit('复位'); await flush()
  store.cancel()
  progress({ kind: 'message', text: '迟到内容' }); finish(plan)
  await running
  expect(store.history[0].message).not.toBe('迟到内容')
  expect(store.phase).toBe('cancelled')
  expect(mock.call.mock.calls.some(([name]) => name === 'take')).toBe(false)
})

it('does not pulse green for an explanation without actions or a failure', async () => {
  await store.start()
  mock.translate.mockResolvedValueOnce({ message: '请说明目标', steps: [] })
  await store.submit('不明确')
  expect(store.phase).toBe('idle')
  mock.translate.mockRejectedValueOnce(Error('模型断流'))
  await store.submit('复位')
  expect(store.phase).toBe('error')
  expect(store.history).toHaveLength(2)
})

it('keeps direct mode history-free while retaining execution feedback', async () => {
  direct = true; await store.start()
  const running = store.submit('status'); await flush()
  expect(store.phase).toBe('executing')
  expect(store.history).toEqual([])
  expect(store.showRecovery).toBe(false)
  pending.set('op-1', true); await vi.advanceTimersByTimeAsync(200); await running
  expect(store.phase).toBe('success')
  expect(mock.translate).not.toHaveBeenCalled()
})

it('keeps 50 local records and does not let an old success timer overwrite a new request', async () => {
  await store.start()
  mock.translate.mockResolvedValue({ message: '已查询', steps: [{ action: 'status' }] })
  for (let i = 0; i < 51; i++) await store.submit(`查询${i}`)
  expect(store.history).toHaveLength(50)
  expect(store.history[0].input).toBe('查询1')
  mock.translate.mockResolvedValue(plan)
  const running = store.submit('复位'); await flush()
  await vi.advanceTimersByTimeAsync(650)
  expect(store.phase).toBe('executing')
  store.cancel(); await vi.advanceTimersByTimeAsync(200); await running
})

it('leaves an unknown submission neutral when manually unlocked, without success feedback', async () => {
  await store.start()
  const original = mock.call.getMockImplementation()!
  mock.call.mockImplementation((name, ...args) => name === 'take' ? Promise.reject(Error('network')) : original(name, ...args))
  await store.submit('复位')
  expect(store.phase).toBe('pending')
  store.acknowledgeUnknown()
  expect(store.phase).toBe('idle')
  expect(store.showRecovery).toBe(false)
})

it('does not let a late manual receipt repaint a newer AI request as successful', async () => {
  await store.start()
  const first = store.submit('复位'); await flush()
  store.cancel(); await vi.advanceTimersByTimeAsync(200); await first
  let receipt!: (value: Payload) => void, translation!: (value: CommandPlan) => void
  mock.call.mockImplementationOnce(() => new Promise(resolve => { receipt = resolve }))
  const checking = store.reconcile()
  mock.translate.mockImplementation(() => new Promise(resolve => { translation = resolve }))
  const second = store.submit('查询状态'); await flush()
  expect(store.phase).toBe('waiting')
  receipt({ operation_id: 'op-1', status: 'succeeded', result: state }); await checking
  expect(store.phase).toBe('waiting')
  translation({ message: '当前状态', steps: [{ action: 'status' }] }); await second
})

it('preserves the unknown operation record while read-only queries remain available', async () => {
  await store.start()
  const original = mock.call.getMockImplementation()!
  mock.call.mockImplementation((name, ...args) => name === 'take' ? Promise.reject(Error('network')) : original(name, ...args))
  await store.submit('复位')
  mock.translate.mockResolvedValue({ message: '当前状态', steps: [{ action: 'status' }] })
  await store.submit('查询状态')
  expect(store.history[0].outcome).toBe('pending')
  expect(store.history[1].outcome).toBe('success')
  expect(store.phase).toBe('pending')
  store.acknowledgeUnknown()
  expect(store.history[0].outcome).toBe('neutral')
  expect(store.history[1].outcome).toBe('success')
})

it('toolbar pause and resume use the guarded executor without calling AI', async () => {
  await store.start()
  state = { ...state, state: 'running' }
  await vi.advanceTimersByTimeAsync(1000)
  expect(store.policyRunning).toBe(true)
  expect(store.canTogglePause).toBe(true)
  const pausing = store.togglePause(); await flush()
  expect(store.phase).toBe('executing')
  pending.set('op-1', true); await vi.advanceTimersByTimeAsync(200)
  pending.set('op-2', true); await vi.advanceTimersByTimeAsync(200); await pausing
  expect(mock.call).toHaveBeenCalledWith('command', expect.objectContaining({ line: 'pause' }), expect.anything())
  await vi.advanceTimersByTimeAsync(1000)
  expect(store.policyRunning).toBe(false)
  const resuming = store.togglePause(); await flush()
  pending.set('op-3', true); await vi.advanceTimersByTimeAsync(200); await resuming
  expect(mock.call).toHaveBeenCalledWith('return', expect.anything(), expect.anything())
  expect(store.phase).toBe('success')
  expect(mock.translate).not.toHaveBeenCalled()
  expect(store.history).toEqual([])
})

it('manual pause supersedes in-flight AI without allowing its late reset plan to execute', async () => {
  state = { ...state, state: 'running' }
  await store.start(); await flush()
  let finish!: (value: CommandPlan) => void, progress!: (value: object) => void
  mock.translate.mockImplementation((...args) => { progress = args[6]; return new Promise(resolve => { finish = resolve }) })
  const translating = store.submit('复位'); await flush()
  expect(store.canTogglePause).toBe(true)
  const pausing = store.togglePause(); await flush()
  progress({ kind: 'message', text: '过期复位指令' }); finish(plan); await translating
  expect(store.phase).toBe('executing')
  expect(store.history[0]).toMatchObject({ outcome: 'cancelled', message: '' })
  pending.set('op-1', true); await vi.advanceTimersByTimeAsync(200)
  pending.set('op-2', true); await vi.advanceTimersByTimeAsync(200); await pausing
  expect(mock.call.mock.calls.filter(([name]) => name === 'command').map(([, args]) => args.line)).toEqual(['pause'])
})

it('a failed RC receipt ends in error without completing later actions', async () => {
  await store.start()
  const original = mock.call.getMockImplementation()!
  mock.call.mockImplementation((name, ...args) => name === 'get_operation'
    ? Promise.resolve({ operation_id: 'op-1', status: 'failed', error: '接管失败' }) : original(name, ...args))
  const running = store.submit('复位'); await vi.advanceTimersByTimeAsync(200); await running
  expect(store.phase).toBe('error')
  expect(store.history[0].outcome).toBe('error')
  expect(store.showRecovery).toBe(false)
  expect(mock.call.mock.calls.some(([name]) => name === 'command')).toBe(false)
})

it('restores a policy episode and keeps terminated results neutral on manual reconciliation', async () => {
  sessionStorage.setItem('rc-pending-operation', JSON.stringify({ id: 'episode', kind: 'policy_episode', run: 'run' }))
  await store.start()
  mock.call.mockResolvedValueOnce({ operation_id: 'episode', status: 'succeeded', finished: true,
    result: { outcome: 'terminated', message: '目标不可达' } })
  await store.reconcile()
  expect(mock.call).toHaveBeenCalledWith('get_policy_result', { operation_id: 'episode' })
  expect(store.phase).toBe('idle')
  expect(store.message).toContain('终止')
  expect(store.message).toContain('旧清单不会继续')
  expect(store.showRecovery).toBe(false)
})
