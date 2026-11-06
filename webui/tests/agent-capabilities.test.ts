import { expect, it } from 'vitest'
import { discoverCapabilities, validateDecision, prepareToolCall } from '../src/lib/agent-capabilities'
import { fixtureGateway, decision, tool } from './agent-fixtures'

it('refreshes all descriptions, state, exact instances and grammar before each model call', async () => {
  const g = fixtureGateway()
  const first = await discoverCapabilities(g)
  expect(first.tools.map(t => t.name)).not.toContain('tui_command')
  expect(first.tools.map(t => t.name)).not.toContain('cancel_tui_command')
  expect(first.policy_commands.policies.map(p => p.instance_id)).toEqual(['example-0', 'example-1'])
  expect(first.tools.find(t => t.name === 'send_command')!.inputSchema.properties).not.toHaveProperty('include_images')
  expect(first.tools.find(t => t.name === 'policy_command')!.inputSchema.properties).not.toHaveProperty('expected_control')
  g.policy_commands.policies[1].commands.push({ ...g.policy_commands.policies[1].commands[0], name: 'new-task', help: '新注册任务' })
  const next = await discoverCapabilities(g, first.status)
  expect(next.policy_commands.policies[1].commands.at(-1)?.help).toBe('新注册任务')
  expect(g.catalog).toHaveBeenCalledTimes(2)
  for (const name of ['policies', 'policy_commands', 'help', 'props']) expect(g.call.mock.calls.filter(([n]) => n === name)).toHaveLength(2)
})

it('rejects incomplete discovery and inconsistent snapshots', async () => {
  const g = fixtureGateway()
  g.catalog.mockResolvedValueOnce({ instructions: '', tools: [] })
  await expect(discoverCapabilities(g)).rejects.toThrow(/能力|目录/)
  const before = { ...g.state }
  g.state.generation++
  await expect(discoverCapabilities(g, before)).rejects.toThrow(/变化|代次/)
  const missing = fixtureGateway()
  delete (missing.policy_commands.policies[0].commands[0] as { timeout_s?: number }).timeout_s
  await expect(discoverCapabilities(missing)).rejects.toThrow(/描述不完整/)
  missing.policy_commands.policies[0].commands[0].timeout_s = -1 // SDK's declared unlimited-handler deadline.
  await expect(discoverCapabilities(missing)).resolves.toBeTruthy()
})

it('validates current schemas and manifest-native types before any tool is submitted', async () => {
  const snapshot = await discoverCapabilities(fixtureGateway())
  expect(validateDecision(decision('policy_command', { instance_id: 'example-1', command: 'set-chunk-size', args: [6] }), snapshot).tool_call?.name).toBe('policy_command')
  for (const candidate of [
    decision('missing'), decision('tui_command', { text: 'exit' }),
    decision('policy_command', { instance_id: 'example', command: 'set-message', args: ['x'] }),
    decision('policy_command', { instance_id: 'example-1', command: 'set-chunk-size', args: ['6'] }),
    decision('policy_command', { instance_id: 'example-1', command: 'set-chunk-size', args: [true] }),
    decision('take', { expected_control: { run_id: 'forged' } }),
    decision('state', { include_images: true }), decision('command', { line: 'policy example-1 set-message x' }),
    decision('show', { scope: 'invalid' }), decision('command', { line: 'example-1-set-message x' }),
  ]) expect(() => validateDecision(candidate, snapshot)).toThrow()
  snapshot.policy_commands.policies[1].runnable = false
  expect(() => validateDecision(decision('policy_command', { instance_id: 'example-1', command: 'set-message', args: ['x'] }), snapshot)).toThrow()
})

it('refuses unguarded legacy writes and supplies host-owned wait and image settings', async () => {
  const g = fixtureGateway()
  const snapshot = await discoverCapabilities(g)
  const waiting = prepareToolCall(decision('return_and_wait').tool_call, snapshot)
  expect(waiting.args).toMatchObject({ timeout_s: 0, wait: false })
  const motion = prepareToolCall(decision('send_command', { commands: 'left:up:0.01' }).tool_call, snapshot)
  expect(motion.args).toMatchObject({ include_images: false, wait: false })
  g.catalog.mockResolvedValueOnce({ instructions: 'legacy', tools: [tool('status'), ...snapshot.tools.filter(t => t.name !== 'status')] })
  const legacy = await discoverCapabilities(g)
  expect(() => prepareToolCall(decision('take').tool_call, legacy)).toThrow(/升级|保护/)
})


it('accepts current RC command paths and rejects removed aliases before submission', async () => {
  const snapshot = await discoverCapabilities(fixtureGateway())
  for (const line of ['status', 'resume', 'policy list', 'policy help example', 'policy switch example --reset',
    'config rc show', 'config rc set frequency_hz 50', 'config rc save', 'global show',
    'global set phase "pick"', 'global remove phase', 'pose save home', 'pose restore home', 'record', 'record start', 'record stop']) {
    expect(validateDecision(decision('command', { line }), snapshot).tool_call?.arguments.line).toBe(line)
  }
  for (const line of ['take', 'return', 'quit', 'switch example', 'show rc', 'set rc.frequency_hz 50',
    'save-config example', 'retry-reload example', 'example-1-set-message x', 'record on', 'record off',
    'record toggle', 'mcp take', 'camera show', 'monitor example', 'snapshot', 'policy manage',
    'policy example-1 config set params.message x', 'status extra']) {
    expect(() => validateDecision(decision('command', { line }), snapshot)).toThrow()
  }
})


it('treats policy_config show as read-only and preserves raw values for guarded writes', async () => {
  const snapshot = await discoverCapabilities(fixtureGateway())
  for (const args of [{ instance_id: 'example-1' }, { instance_id: 'example-1', action: 'show' }]) {
    expect(prepareToolCall(decision('policy_config', args).tool_call, snapshot).readOnly).toBe(true)
  }
  const raw = 'a value with "quotes" and spaces'
  const prepared = prepareToolCall(decision('policy_config', {
    instance_id: 'example-1', action: 'set', key: 'params.message', value_text: raw,
  }).tool_call, snapshot)
  expect(prepared.readOnly).toBe(false)
  expect(prepared.args).toMatchObject({ value_text: raw, wait: false })
  expect(prepareToolCall(decision('policy_config', { instance_id: 'example-1', action: 'save' }).tool_call, snapshot).readOnly).toBe(false)
})

it('allows only explicitly failed instances from a backend recovery set', async () => {
  const snapshot = await discoverCapabilities(fixtureGateway())
  snapshot.status.retry_reload_instance = 'example-0'
  snapshot.status.retry_reload_instances = ['example-0', 'example-1']
  expect(() => validateDecision(decision('retry_reload', { instance_id: 'example-1' }), snapshot)).not.toThrow()
  expect(() => validateDecision(decision('retry_reload', { instance_id: 'example-2' }), snapshot)).toThrow()
})
