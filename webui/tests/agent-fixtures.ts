import { vi } from 'vitest'
import type { RcStatus } from '../src/lib/commands'
import type { ToolDefinition, CapabilitySnapshot } from '../src/lib/agent-capabilities'

export const initialStatus: RcStatus = { run_id: 'run', state: 'paused', control_owner: 'policy', control_epoch: 1, generation: 1, returning: false }
export const tool = (name: string, read = true, properties: Record<string, unknown> = {}, required: string[] = []): ToolDefinition => ({
  name, description: `Description for ${name}`, annotations: { readOnlyHint: read },
  inputSchema: { type: 'object', properties: { ...properties, ...(!read ? { expected_control: { type: 'object' }, wait: { type: 'boolean' } } : {}) }, required },
})
export const tools = [
  ...['status', 'policies', 'policy_commands', 'help', 'props', 'events'].map(name => tool(name)),
  tool('state', true, { include_images: { type: 'boolean' } }),
  tool('show', true, { scope: { enum: ['rc', 'policy', 'global'] } }, ['scope']),
  tool('get_operation', true, { operation_id: { type: 'string' } }, ['operation_id']),
  tool('get_policy_result', true, { operation_id: { type: 'string' } }),
  tool('take', false), tool('return', false), tool('return_and_wait', false, { timeout_s: { type: 'number' } }),
  tool('command', false, { line: { type: 'string' } }, ['line']),
  tool('record', false, { action: { enum: ['status', 'on', 'off', 'toggle'] } }),
  tool('policy_chain', false, { policies: { type: 'array', items: { type: 'string' } }, action: { enum: ['apply', 'save'] } }, ['policies']),
  tool('policy_command', false, { instance_id: { type: 'string' }, command: { type: 'string' }, args: { type: 'array' } }, ['instance_id', 'command']),
  tool('retry_reload', false, { instance_id: { type: 'string' } }, ['instance_id']),
  tool('send_command', false, { commands: { type: 'string' }, include_images: { type: 'boolean' } }, ['commands']),
  tool('move_eef', false, { targets: { type: 'object' } }, ['targets']), tool('move_joints', false, { targets: { type: 'object' } }, ['targets']),
  tool('tui_command', false, { text: { type: 'string' } }), tool('cancel_tui_command', false, { operation_id: { type: 'string' } }),
]
export function fixtureGateway() {
  const state = { ...initialStatus }
  const policy_commands = { policies: [0, 1].map(i => ({ source_id: 'example', instance_id: `example-${i}`, loaded: true, runnable: true, commands: [
    { name: 'set-message', alias: `example-${i}-set-message`, args: ['str'], help: '热更新消息', effect: 'update_config', requires_take: true, timeout_s: 5 },
    { name: 'set-chunk-size', alias: `example-${i}-set-chunk-size`, args: ['int'], help: '冷重载本实例', effect: 'reload_policy', requires_take: true, timeout_s: 5 },
  ] })), errors: [] }
  const catalog = vi.fn(async () => ({ instructions: 'Read-only until take. Recording needs no take.', tools: structuredClone(tools) }))
  const status = vi.fn(async () => ({ ...state }))
  const call = vi.fn(async (name: string, _args: Record<string, unknown> = {}, _expected?: RcStatus): Promise<Record<string, unknown>> => {
    if (name === 'status') return { ...state }
    if (name === 'policies') return { result: [{ id: 'example', valid: true }] }
    if (name === 'policy_commands') return structuredClone(policy_commands)
    if (name === 'help') return { result: 'set <实例ID>.path <JSON>; switch; pause; resume; exit' }
    if (name === 'props') return { units: { epos: 'meters/radians' } }
    return {}
  })
  return { state, policy_commands, catalog, status, call }
}
export const decision = (name: string, args: Record<string, unknown> = {}) => ({ message: `使用 ${name}`, tool_call: { name, arguments: args } })
export const finalDecision = { message: '已完成请求', tool_call: null }
export type Snapshot = CapabilitySnapshot
