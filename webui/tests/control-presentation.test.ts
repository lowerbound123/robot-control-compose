import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { Payload, RcStatus } from '../src/lib/commands'
import type { AgentDecision } from '../src/lib/agent-capabilities'
import { decision, finalDecision, fixtureGateway } from './agent-fixtures'
import { useControlStore } from '../src/stores/control'

const mock = vi.hoisted(() => ({ status: vi.fn(), call: vi.fn(), catalog: vi.fn(), close: vi.fn(async () => {}), decide: vi.fn(), download: vi.fn() }))
vi.mock('../src/lib/download', () => ({ downloadBlob: mock.download, fileTime: () => 'test' }))
vi.mock('../src/lib/mcp', () => ({ McpGateway: class { status = mock.status; call = mock.call; catalog = mock.catalog; close = mock.close } }))
// Keep the live agent and executor path; control only the model decisions and
// gateway receipts so presentation, cancellation and recovery stay deterministic.
vi.mock('../src/lib/translator', async importOriginal => ({
  ...await importOriginal<typeof import('../src/lib/translator')>(), requestDecision: mock.decide,
}))
let state: RcStatus, store: ReturnType<typeof useControlStore>, pending: Map<string, boolean>, rejected: Set<string>, id: number, direct: boolean
const takeDecision = decision('take')
const statusDecision = decision('status')

beforeEach(() => {
  vi.useFakeTimers()
  vi.resetAllMocks()
  setActivePinia(createPinia())
  state = { run_id: 'run', state: 'paused', control_owner: 'policy', control_epoch: 1, generation: 1, returning: false }
  pending = new Map(); rejected = new Set(); id = 0; direct = false
  vi.stubGlobal('window', { location: { origin: 'http://localhost' } })
  vi.stubGlobal('document', { hidden: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })
  const saved = new Map()
  vi.stubGlobal('sessionStorage', { getItem: (key: string) => saved.get(key) || null, setItem: (key: string, value: string) => saved.set(key, value), removeItem: (key: string) => saved.delete(key) })
  vi.stubGlobal('fetch', vi.fn(async (url: string) => new Response(JSON.stringify(url === '/config.json'
    ? { llm: !direct, enabled: !direct, model: direct ? '' : 'test', timeout_s: 3 }
    : url === '/health' ? state : { cameras: {} }))))
  const fixture = fixtureGateway()
  mock.catalog.mockImplementation(fixture.catalog)
  mock.status.mockImplementation(async () => ({ ...state }))
  mock.call.mockImplementation(async (name: string, args?: Payload) => {
    if (['policies', 'policy_commands', 'help', 'props'].includes(name)) return fixture.call(name, args)
    if (name === 'status') return { ...state }
    if (name === 'get_operation') return { operation_id: args!.operation_id, status: pending.get(String(args!.operation_id)) ? (rejected.has(String(args!.operation_id)) ? 'failed' : 'succeeded') : 'running', error: rejected.has(String(args!.operation_id)) ? '请先 mcp return' : undefined, result: { ...state } }
    if (name === 'take') state = { ...state, control_owner: 'mcp', control_epoch: state.control_epoch + 1, generation: state.generation + 1 }
    if (name === 'command') state = { ...state, state: 'paused', generation: state.generation + 1 }
    if (name === 'return') state = { ...state, state: 'running', control_owner: 'policy', control_epoch: state.control_epoch + 1, generation: state.generation + 1 }
    const resumeDenied = name === 'tui_command' && args?.text === 'resume' && state.control_owner === 'mcp'
    if (name === 'tui_command' && !resumeDenied) state = { ...state, state: args?.text === 'resume' ? 'running' : 'paused',
      control_owner: state.control_owner, generation: state.generation + 1 }
    const operation_id = `op-${++id}`
    pending.set(operation_id, false)
    if (resumeDenied) rejected.add(operation_id)
    return { operation_id, status: 'queued' }
  })
  mock.decide.mockImplementation(async (_config, prompt) => prompt.exchanges.length === 0 ? takeDecision
    : prompt.exchanges.length === 1 ? decision('command', { line: 'reset' }) : finalDecision)
  store = useControlStore()
})
afterEach(() => { store.stop(); vi.useRealTimers(); vi.unstubAllGlobals() })
async function flush() { await vi.advanceTimersByTimeAsync(0) }

it('shows real streamed text, stays orange across intermediate receipts and pulses only after all tools', async () => {
  await store.start()
  let finish!: (value: AgentDecision) => void
  mock.decide.mockImplementationOnce((...args) => {
    args[2].onProgress({ kind: 'reasoning', text: '检查控制状态' })
    args[2].onProgress({ kind: 'message', text: '准备' })
    return new Promise(resolve => { finish = resolve })
  })
  const running = store.submit('复位')
  await flush()
  expect(store.phase).toBe('streaming')
  expect(store.history[0]).toMatchObject({ input: '复位', reasoning: '检查控制状态', message: '准备' })
  expect(mock.call.mock.calls.some(([name]) => name === 'take')).toBe(false)
  finish(takeDecision); await flush()
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

it.each([false, true])('associates recovery with the original AI request after a subsequent query (unknown=%s)', async unknown => {
  await store.start()
  if (unknown) {
    const original = mock.call.getMockImplementation()!
    mock.call.mockImplementation((name, ...args) => name === 'take' ? Promise.reject(Error('network')) : original(name, ...args))
    await store.submit('复位')
  } else {
    const running = store.submit('复位'); await flush()
    store.cancel(); await vi.advanceTimersByTimeAsync(200); await running
  }
  mock.decide.mockImplementation(async (_config, prompt) => prompt.exchanges.length ? finalDecision : statusDecision)
  await store.submit('查询状态')
  if (unknown) store.acknowledgeUnknown()
  else { pending.set('op-1', true); await store.reconcile() }
  store.exportLog()
  const log = JSON.parse(await (mock.download.mock.calls.at(-1)![0] as Blob).text())
  const event = log.events.find((event: any) => event.type === (unknown ? 'recovery.acknowledged' : 'recovery.result'))
  expect(event.requestId).toBe(1)
})

it('ignores late model text and a late tool decision after cancellation', async () => {
  await store.start()
  let finish!: (value: AgentDecision) => void, progress!: (value: object) => void
  mock.decide.mockImplementationOnce((...args) => { progress = args[2].onProgress; return new Promise(resolve => { finish = resolve }) })
  const running = store.submit('复位'); await flush()
  store.cancel()
  progress({ kind: 'message', text: '迟到内容' }); finish(takeDecision)
  await running
  expect(store.history[0].message).not.toBe('迟到内容')
  expect(store.phase).toBe('cancelled')
  expect(mock.call.mock.calls.some(([name]) => name === 'take')).toBe(false)
})

it('does not pulse green for an explanation without actions or a failure', async () => {
  await store.start()
  mock.decide.mockResolvedValueOnce({ message: '请说明目标', tool_call: null })
  await store.submit('不明确')
  expect(store.phase).toBe('idle')
  mock.decide.mockRejectedValueOnce(Error('模型断流'))
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
  expect(mock.decide).not.toHaveBeenCalled()
})

it('keeps 50 local records and does not let an old success timer overwrite a new request', async () => {
  await store.start()
  mock.decide.mockImplementation(async (_config, prompt) => prompt.exchanges.length ? finalDecision : statusDecision)
  for (let i = 0; i < 51; i++) await store.submit(`查询${i}`)
  expect(store.history).toHaveLength(50)
  expect(store.history[0].input).toBe('查询1')
  mock.decide.mockImplementation(async (_config, prompt) => prompt.exchanges.length === 0 ? takeDecision
    : prompt.exchanges.length === 1 ? decision('command', { line: 'reset' }) : finalDecision)
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
  let receipt!: (value: Payload) => void, translation!: (value: AgentDecision) => void
  mock.call.mockImplementationOnce(() => new Promise(resolve => { receipt = resolve }))
  const checking = store.reconcile()
  mock.decide.mockImplementationOnce(() => new Promise(resolve => { translation = resolve }))
    .mockResolvedValue(finalDecision)
  const second = store.submit('查询状态'); await flush()
  expect(store.phase).toBe('waiting')
  receipt({ operation_id: 'op-1', status: 'succeeded', result: state }); await checking
  expect(store.phase).toBe('waiting')
  translation(statusDecision); await second
})

it('preserves the unknown operation record while read-only queries remain available', async () => {
  await store.start()
  const original = mock.call.getMockImplementation()!
  mock.call.mockImplementation((name, ...args) => name === 'take' ? Promise.reject(Error('network')) : original(name, ...args))
  await store.submit('复位')
  mock.decide.mockImplementation(async (_config, prompt) => prompt.exchanges.length ? finalDecision : statusDecision)
  await store.submit('查询状态')
  expect(store.history[0].outcome).toBe('pending')
  expect(store.history[1].outcome).toBe('success')
  expect(store.phase).toBe('pending')
  store.acknowledgeUnknown()
  expect(store.history[0].outcome).toBe('neutral')
  expect(store.history[1].outcome).toBe('success')
})

for (const owner of ['policy', 'mcp']) for (const llm of [true, false]) {
  it.each(['pause', 'resume', 'reset'] as const)(`toolbar %s uses one human TUI command (owner=${owner}, llm=${llm})`, async action => {
    direct = !llm
    state = { ...state, state: action === 'resume' ? 'paused' : 'running', control_owner: owner }
    const before = { ...state }
    await store.start(); await flush()
    expect(store.canReset).toBe(true)
    expect(store.canTogglePause).toBe(true)
    const running = action === 'reset' ? store.reset() : store.togglePause(); await flush()
    expect(store.phase).toBe('executing')
    expect(mock.call).toHaveBeenCalledWith('tui_command', { text: action, wait: false }, before)
    pending.set('op-1', true); await vi.advanceTimersByTimeAsync(200); await running
    expect(mock.call.mock.calls.filter(([name]) => ['take', 'return', 'command', 'return_and_wait'].includes(name))).toEqual([])
    expect(mock.call.mock.calls.filter(([name]) => name === 'tui_command')).toHaveLength(1)
    expect(store.phase).toBe(action === 'resume' && owner === 'mcp' ? 'error' : 'success')
    if (action === 'resume' && owner === 'mcp') expect(state.state).toBe('paused')
    expect(mock.decide).not.toHaveBeenCalled()
    expect(store.history).toEqual([])
    expect(state.control_owner).toBe(owner)
  })
}

it('manual pause supersedes in-flight AI without allowing its late reset decision to execute', async () => {
  state = { ...state, state: 'running' }
  await store.start(); await flush()
  let finish!: (value: AgentDecision) => void, progress!: (value: object) => void
  mock.decide.mockImplementationOnce((...args) => { progress = args[2].onProgress; return new Promise(resolve => { finish = resolve }) })
  const translating = store.submit('复位'); await flush()
  expect(store.canTogglePause).toBe(true)
  const pausing = store.togglePause(); await flush()
  progress({ kind: 'message', text: '过期复位指令' }); finish(takeDecision); await translating
  expect(store.phase).toBe('executing')
  expect(store.history[0]).toMatchObject({ outcome: 'cancelled', message: '' })
  pending.set('op-1', true); await vi.advanceTimersByTimeAsync(200); await pausing
  expect(mock.call.mock.calls.filter(([name]) => name === 'tui_command').map(([, args]) => args.text)).toEqual(['pause'])
  expect(mock.call.mock.calls.some(([name]) => name === 'take' || name === 'command')).toBe(false)
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
