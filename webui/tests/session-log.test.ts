import { expect, it, vi } from 'vitest'
import { SessionLog } from '../src/lib/session-log'
import { requestDecision } from '../src/lib/translator'
import { discoverCapabilities } from '../src/lib/agent-capabilities'
import { fixtureGateway } from './agent-fixtures'
import { McpGateway } from '../src/lib/mcp'

it('retains all requests independently of display history and exports immutable snapshots', () => {
  const log = new SessionLog()
  for (let id = 1; id <= 61; id++) log.scope({ requestId: id })('request.started', { input: `请求${id}` })
  const scope = log.scope({ requestId: 1, roundId: 2 })
  const payload = { operation_id: 'op', result: { message: '完整结果'.repeat(3000) } }
  scope('mcp.response', payload)
  const first = log.snapshot()
  payload.result.message = 'changed'
  scope('request.cancelled', {})
  expect(first.events).toHaveLength(62)
  expect(log.snapshot().events).toHaveLength(63)
  expect(first.events.at(-1)).toMatchObject({ requestId: 1, roundId: 2, sequence: 62, data: { result: { message: '完整结果'.repeat(3000) } } })
  expect(first).toMatchObject({ formatVersion: 1, complete: true })
})

it('marks recording failures without throwing into the control flow', () => {
  const log = new SessionLog()
  const circular: any = {}; circular.self = circular
  expect(() => log.scope({ requestId: 1 })('broken', circular)).not.toThrow()
  log.scope({ requestId: 1 })('request.finished', { status: 'success' })
  expect(log.snapshot()).toMatchObject({ complete: false, events: [{ type: 'request.finished' }] })
  expect(log.error).toBeTruthy()
})

it.each([false, true])('records raw responses and invalid tool attempts before validation (stream=%s)', async stream => {
  const log = new SessionLog(), g = fixtureGateway()
  const snapshot = await discoverCapabilities(g, g.state)
  const content = JSON.stringify({ message: '尝试', tool_call: { name: 'missing-tool', arguments: { value: 123 } } })
  const body = stream ? `data: ${JSON.stringify({ choices: [{ index: 0, delta: { content, reasoning_content: '真实思考' } }] })}\n\ndata: [DONE]\n\n`
    : JSON.stringify({ choices: [{ message: { content, reasoning: '真实思考' }, finish_reason: 'stop' }] })
  await expect(requestDecision({ llm: true, enabled: true, model: 'test', timeout_s: 1 }, { input: '测试', snapshot, history: [], exchanges: [] }, {
    audit: log.scope({ requestId: 1, roundId: 1 }),
    fetcher: async () => new Response(body, { headers: { 'Content-Type': stream ? 'text/event-stream' : 'application/json' } }),
  })).rejects.toThrow()
  const events = log.snapshot().events
  expect(events.some(event => event.type === 'model.request')).toBe(true)
  expect(events.filter(event => event.type === 'model.response.chunk').map(event => (event.data as any).text).join('')).toBe(body)
  expect(events.find(event => event.type === 'model.decision')?.data).toMatchObject({ tool_call: { name: 'missing-tool' } })
  expect(events.at(-1)?.type).toBe('model.error')
})

it('keeps a partial response when the stream ends without DONE', async () => {
  const log = new SessionLog(), g = fixtureGateway()
  const body = 'data: {"choices":[{"delta":{"content":"{\\"message\\":\\"一半"}}]}\n\n'
  await expect(requestDecision({ llm: true, enabled: true, model: 'test', timeout_s: 1 }, {
    input: '测试', snapshot: await discoverCapabilities(g, g.state), history: [], exchanges: [],
  }, { audit: log.scope({ requestId: 1 }), fetcher: async () => new Response(body, { headers: { 'Content-Type': 'text/event-stream' } }) })).rejects.toThrow()
  expect(log.snapshot().events.find(event => event.type === 'model.response.chunk')?.data).toEqual({ text: body })
})

it('records the bounded local proxy error body before reporting HTTP failure', async () => {
  const log = new SessionLog(), g = fixtureGateway(), text = '{"error":"AI upstream unavailable"}'
  await expect(requestDecision({ llm: true, enabled: true, model: 'test', timeout_s: 1 }, {
    input: '测试', snapshot: await discoverCapabilities(g, g.state), history: [], exchanges: [],
  }, { audit: log.scope({ requestId: 1 }), fetcher: async () => new Response(text, { status: 502 }) })).rejects.toThrow('HTTP 502')
  expect(log.snapshot().events.find(event => event.type === 'model.response.chunk')?.data).toEqual({ text })
})

it('binds a late MCP response to the initiating scope and records actual guarded parameters', async () => {
  const log = new SessionLog()
  let scope = log.scope({ requestId: 1, roundId: 2 })
  let resolve!: (response: any) => void
  const g = new McpGateway(new URL('http://localhost/mcp'), () => scope)
  vi.spyOn(g as any, 'connection').mockResolvedValue({ callTool: () => new Promise(r => { resolve = r }), close: async () => {} })
  const state = { run_id: 'run', control_epoch: 2, generation: 3, state: 'paused', control_owner: 'mcp', returning: false }
  const pending = g.call('take', {}, state)
  await vi.waitFor(() => expect(resolve).toBeDefined())
  scope = log.scope({ requestId: 2 })
  resolve({ structuredContent: { operation_id: 'op', status: 'succeeded' } })
  await pending
  expect(log.snapshot().events.every(event => event.requestId === 1 && event.roundId === 2)).toBe(true)
  expect(log.snapshot().events.find(event => event.type === 'mcp.request')?.data).toMatchObject({ name: 'take', arguments: { expected_control: { run_id: 'run', generation: 3 } } })
  expect(log.snapshot().events.at(-1)?.data).toMatchObject({ response: { structuredContent: { operation_id: 'op' } } })
})
