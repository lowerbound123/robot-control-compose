import { expect, it, vi } from 'vitest'
import { runAgent } from '../src/lib/agent'
import { PlanExecutor } from '../src/lib/commands'
import { fixtureGateway, decision, finalDecision } from './agent-fixtures'

const config = { llm: true, enabled: true, model: 'test', timeout_s: 3 }
function environment() {
  const g = fixtureGateway(), base = g.call.getMockImplementation()!
  g.call.mockImplementation(async (name, args, expected) => {
    if (['take', 'policy_command', 'command'].includes(name)) {
      if (name === 'take') { g.state.control_owner = 'mcp'; g.state.control_epoch++ }
      g.state.generation++
      return { operation_id: `${name}-${g.state.generation}`, status: 'succeeded', result: {}, controller: { ...g.state },
        completion_control: { run_id: g.state.run_id, generation: g.state.generation, control_epoch: g.state.control_epoch } }
    }
    if (name === 'show') return { params: { message: 'new' } }
    return base(name, args, expected)
  })
  return { g, executor: new PlanExecutor(g, { pollMs: 1 }) }
}

it('queries, takes, runs a typed policy command and verifies using fresh descriptions each round', async () => {
  const { g, executor } = environment()
  const choices = [decision('show', { scope: 'policy' }), decision('take'),
    decision('policy_command', { instance_id: 'example-1', command: 'set-message', args: ['new'] }), decision('show', { scope: 'policy' }), finalDecision]
  const decide = vi.fn(async (_config, prompt) => {
    if (prompt.exchanges.length) expect(prompt.exchanges.at(-1).result).toBeTruthy()
    return choices.shift()!
  })
  const result = await runAgent({ config, input: '修改消息并确认', gateway: g, executor, history: [], decide })
  expect(result.message).toBe('已完成请求')
  expect(result.executed).toBe(4)
  expect(decide).toHaveBeenCalledTimes(5)
  expect(g.catalog).toHaveBeenCalledTimes(5)
  expect(g.call.mock.calls.filter(([name]) => name === 'policy_command')[0][1]).toMatchObject({ instance_id: 'example-1', args: ['new'] })
})

it('refreshes instance commands after switching and uses the new exact identity', async () => {
  const { g, executor } = environment(), base = g.call.getMockImplementation()!
  g.call.mockImplementation(async (name, args, expected) => {
    if (name === 'command') g.policy_commands.policies = [{ ...g.policy_commands.policies[0], instance_id: 'example' }]
    return base(name, args, expected)
  })
  const choices = [decision('take'), decision('command', { line: 'policy switch example' }), decision('policy_command', { instance_id: 'example', command: 'set-message', args: ['new'] }), finalDecision]
  const decide = vi.fn(async (_config, prompt) => {
    if (prompt.exchanges.length === 2) expect(prompt.snapshot.policy_commands.policies[0].instance_id).toBe('example')
    return choices.shift()!
  })
  expect((await runAgent({ config, input: '切换后改消息', gateway: g, executor, history: [], decide })).executed).toBe(3)
})

it('limits execution to eight tools and requests one final-only response', async () => {
  const { g, executor } = environment()
  const decide = vi.fn(async (_config, prompt) => prompt.finalOnly ? finalDecision : decision('status'))
  const result = await runAgent({ config, input: '查询', gateway: g, executor, history: [], decide })
  expect(result.executed).toBe(8)
  expect(decide).toHaveBeenCalledTimes(9)
})

it('stops after a lost write, never asking the model to retry', async () => {
  const { g, executor } = environment(), base = g.call.getMockImplementation()!
  g.call.mockImplementation((name, args, expected) => name === 'take' ? Promise.reject(Error('lost response')) : base(name, args, expected))
  const decide = vi.fn(async () => decision('take'))
  await expect(runAgent({ config, input: '接管', gateway: g, executor, history: [], decide })).rejects.toThrow('lost response')
  expect(executor.uncertainSubmission).toBe(true)
  expect(decide).toHaveBeenCalledTimes(1)
})

it('rejects stale final answers and late tool choices after cancellation', async () => {
  for (const cancel of [false, true]) {
    const { g, executor } = environment(), signal = new AbortController()
    const decide = vi.fn(async () => {
      if (cancel) signal.abort()
      else g.state.generation++
      return cancel ? decision('take') : finalDecision
    })
    await expect(runAgent({ config, input: '操作', gateway: g, executor, history: [], signal: signal.signal, decide })).rejects.toThrow()
    expect(g.call.mock.calls.some(([name]) => name === 'take')).toBe(false)
  }
})

it('reports a terminated episode as terminated and stops autonomous continuation', async () => {
  const { g, executor } = environment(); g.state.control_owner = 'mcp'
  const base = g.call.getMockImplementation()!
  g.call.mockImplementation(async (name, args, expected) => name === 'return_and_wait' ? {
    operation_id: 'episode', status: 'succeeded', finished: true, result: { outcome: 'terminated', message: '目标不可达' },
    completion_control: { run_id: g.state.run_id, generation: g.state.generation, control_epoch: g.state.control_epoch },
  } : base(name, args, expected))
  const decide = vi.fn(async () => decision('return_and_wait'))
  const result = await runAgent({ config, input: '运行并报告结果', gateway: g, executor, history: [], decide })
  expect(result.outcome).toBe('terminated')
  expect(result.message).toContain('目标不可达')
  expect(decide).toHaveBeenCalledTimes(1)
})
