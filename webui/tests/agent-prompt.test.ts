import { expect, it, vi } from 'vitest'
import { buildAgentRequest, recentConversation } from '../src/lib/agent-prompt'
import { requestDecision } from '../src/lib/translator'
import { discoverCapabilities } from '../src/lib/agent-capabilities'
import { fixtureGateway, finalDecision } from './agent-fixtures'

it('supplies complete live descriptions, grammar and only bounded ended conversation', async () => {
  const snapshot = await discoverCapabilities(fixtureGateway())
  const history = Array.from({ length: 9 }, (_, i) => ({ input: `任务${i}`, message: `结果${i}`, status: '已完成', outcome: 'success', reasoning: 'SECRET-THINKING' }))
  history.push({ input: '正在生成', message: '', status: '', outcome: 'running', reasoning: '' })
  const selected = recentConversation(history)
  expect(selected).toHaveLength(6)
  expect(selected[0].input).toBe('任务3')
  const body = buildAgentRequest({ llm: true, enabled: true, model: 'test', timeout_s: 30, effort: 'high' }, { input: '修改刚才那个实例', snapshot, history, exchanges: [] })
  const text = JSON.stringify(body)
  expect(text).toContain('热更新消息')
  expect(text).toContain('meters/radians')
  expect(text).toContain('example-1')
  expect(text).not.toContain('SECRET-THINKING')
  expect(body.reasoning_effort).toBe('high')
  expect(body.tools).toBeUndefined()
  expect(body.stream).toBe(true)
})

it('trims history first, labels result excerpts and never truncates tool schemas', async () => {
  const snapshot = await discoverCapabilities(fixtureGateway())
  const history = [{ input: 'x'.repeat(14000), message: 'old', outcome: 'success', status: '' }]
  expect(recentConversation(history)).toEqual([])
  const exchanges = [{ decision: { message: '查询', tool_call: { name: 'show', arguments: { scope: 'global' } } },
    result: { operation_id: 'known', status: 'succeeded', data: '多'.repeat(100000),
      result: { outcome: 'terminated', source: { instance_id: 'exact-target' }, message: '目标不可达' } } }]
  const body = buildAgentRequest({ llm: true, enabled: true, model: 'test', timeout_s: 30 }, { input: '查询', snapshot, history, exchanges })
  expect(new TextEncoder().encode(JSON.stringify(body)).length).toBeLessThanOrEqual(256 * 1024)
  expect(JSON.stringify(body)).toContain('truncated')
  expect(JSON.stringify(body)).toContain('known')
  const result = JSON.parse(body.messages.at(-1)!.content).result
  expect(result.result).toMatchObject({ outcome: 'terminated', source: { instance_id: 'exact-target' }, message: '目标不可达' })
  snapshot.tools[0].description = 'x'.repeat(300000)
  expect(() => buildAgentRequest({ llm: true, enabled: true, model: 'test', timeout_s: 30 }, { input: '查询', snapshot, history, exchanges })).toThrow(/过大/)
})

it('uses the configured effort once and refuses model tools in final-only mode', async () => {
  const snapshot = await discoverCapabilities(fixtureGateway())
  const fetcher = vi.fn(async (_url, init) => {
    const body = JSON.parse(init.body)
    expect(body.reasoning_effort).toBe('xhigh')
    return new Response(JSON.stringify({ choices: [{ message: { content: JSON.stringify(finalDecision) } }] }))
  })
  const config = { llm: true, enabled: true, model: 'test', timeout_s: 30, effort: 'xhigh' as const }
  expect(await requestDecision(config, { input: '查询', snapshot, history: [], exchanges: [], finalOnly: true }, { fetcher })).toEqual(finalDecision)
  expect(fetcher).toHaveBeenCalledTimes(1)
  await expect(requestDecision(config, { input: '查询', snapshot, history: [], exchanges: [], finalOnly: true }, {
    fetcher: async () => new Response(JSON.stringify({ choices: [{ message: { content: '{"message":"再查一次","tool_call":{"name":"status","arguments":{}}}' } }] })),
  })).rejects.toThrow(/上限|总结/)
})
