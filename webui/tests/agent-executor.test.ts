import { expect, it, vi } from 'vitest'
import { PlanExecutor, ToolRejectedError } from '../src/lib/commands'
import { discoverCapabilities, prepareToolCall } from '../src/lib/agent-capabilities'
import { decision, fixtureGateway } from './agent-fixtures'

function receipt(state: Record<string, unknown>, patch = {}) {
  return { operation_id: 'op', status: 'succeeded', controller: { ...state },
    completion_control: { run_id: state.run_id, control_epoch: state.control_epoch, generation: state.generation }, ...patch }
}

it('sends an explicitly selected take with the original guard and accepts its proven new generation', async () => {
  const g = fixtureGateway(), snapshot = await discoverCapabilities(g)
  const original = g.call.getMockImplementation()!
  g.call.mockImplementation(async (name, args, expected) => {
    if (name !== 'take') return original(name, args, expected)
    expect(expected).toEqual(snapshot.status)
    g.state.control_owner = 'mcp'; g.state.generation++; g.state.control_epoch++
    return receipt(g.state)
  })
  const executor = new PlanExecutor(g)
  const result = await executor.runTool(prepareToolCall(decision('take').tool_call, snapshot), snapshot.status)
  expect(result.control).toEqual(g.state)
  expect(executor.uncertainSubmission).toBe(false)
  expect(executor.unresolvedOperation).toBeNull()
})

it('does not auto-take for recording and stops on an unproven or stale completion', async () => {
  const g = fixtureGateway(), snapshot = await discoverCapabilities(g)
  g.call.mockResolvedValue(receipt(g.state))
  const executor = new PlanExecutor(g)
  await executor.runTool(prepareToolCall(decision('record', { action: 'on' }).tool_call, snapshot), snapshot.status)
  expect(g.call.mock.calls.some(([name]) => name === 'take')).toBe(false)
  g.call.mockImplementation(async () => { const old = receipt(g.state); g.state.generation++; return { ...old, controller: { ...g.state } } })
  await expect(executor.runTool(prepareToolCall(decision('take').tool_call, snapshot), snapshot.status)).rejects.toThrow(/代次|变化/)
})

it('persists uncertainty before network acceptance and never resubmits a lost write', async () => {
  const g = fixtureGateway(), snapshot = await discoverCapabilities(g)
  const executor = new PlanExecutor(g), emit = vi.fn(() => { expect(executor.uncertainSubmission).toBe(true) })
  g.call.mockRejectedValue(Error('offline'))
  const call = prepareToolCall(decision('take').tool_call, snapshot)
  await expect(executor.runTool(call, snapshot.status, emit)).rejects.toThrow('offline')
  expect(executor.uncertainSubmission).toBe(true)
  await expect(executor.runTool(call, snapshot.status)).rejects.toThrow(/确认/)
  expect(g.call).toHaveBeenCalledTimes(5) // four discovery reads and this write
})

it('distinguishes rejected admission from an accepted failed receipt', async () => {
  const g = fixtureGateway(), snapshot = await discoverCapabilities(g), executor = new PlanExecutor(g)
  const call = prepareToolCall(decision('take').tool_call, snapshot)
  g.call.mockRejectedValueOnce(new ToolRejectedError('rejected'))
  await expect(executor.runTool(call, snapshot.status)).rejects.toThrow('rejected')
  expect(executor.uncertainSubmission).toBe(false)
  g.call.mockResolvedValueOnce(receipt(g.state, { status: 'failed', error: 'failed' }))
  await expect(executor.runTool(call, snapshot.status)).rejects.toThrow('failed')
  expect(executor.unresolvedOperation).toBeNull()
})

it('polls policy episodes with get_policy_result and preserves terminated business outcomes', async () => {
  const g = fixtureGateway(); g.state.control_owner = 'mcp'
  const snapshot = await discoverCapabilities(g), executor = new PlanExecutor(g, { pollMs: 1 })
  g.call.mockImplementation(async (name, args) => {
    if (name === 'return_and_wait') { expect(args?.timeout_s).toBe(0); return { operation_id: 'episode', status: 'running', finished: false } }
    expect(name).toBe('get_policy_result')
    return receipt(g.state, { operation_id: 'episode', finished: true, result: { outcome: 'terminated', message: '停止原因' } })
  })
  const result = await executor.runTool(prepareToolCall(decision('return_and_wait').tool_call, snapshot), snapshot.status)
  expect(result.payload.result).toEqual({ outcome: 'terminated', message: '停止原因' })
  expect(g.call.mock.calls.some(([name]) => name === 'get_operation')).toBe(false)
})

it('keeps a timed-out episode recoverable with the right query kind', async () => {
  const g = fixtureGateway(); g.state.control_owner = 'mcp'
  const snapshot = await discoverCapabilities(g), executor = new PlanExecutor(g, { pollMs: 1, receiptTimeoutMs: 5 })
  g.call.mockResolvedValue({ operation_id: 'episode', status: 'running', finished: false })
  await expect(executor.runTool(prepareToolCall(decision('return_and_wait').tool_call, snapshot), snapshot.status)).rejects.toThrow(/等待/)
  expect(executor.pendingKind).toBe('policy_episode')
  g.call.mockResolvedValue(receipt(g.state, { operation_id: 'episode', finished: true }))
  await executor.reconcile()
  expect(g.call.mock.calls.at(-1)).toEqual(['get_policy_result', { operation_id: 'episode' }])
  expect(executor.pendingKind).toBeNull()
})

it('stops on shutdown acceptance without treating disconnection as completion', async () => {
  const g = fixtureGateway(); g.state.control_owner = 'mcp'
  const snapshot = await discoverCapabilities(g), executor = new PlanExecutor(g)
  g.call.mockResolvedValue({ operation_id: 'exit', status: 'accepted' })
  const result = await executor.runTool(prepareToolCall(decision('command', { line: 'exit' }).tool_call, snapshot), snapshot.status)
  expect(result.shutdownAccepted).toBe(true)
  expect(executor.unresolvedOperation).toBe('exit')
})

it('stops before a write when cancelled during its final state check', async () => {
  const g = fixtureGateway(), snapshot = await discoverCapabilities(g), executor = new PlanExecutor(g)
  const abort = new AbortController()
  g.status.mockImplementationOnce(async () => { abort.abort(); return { ...g.state } })
  const count = g.call.mock.calls.length
  await expect(executor.runTool(prepareToolCall(decision('take').tool_call, snapshot), snapshot.status, undefined, abort.signal)).rejects.toThrow()
  expect(g.call.mock.calls.length).toBe(count)
})
