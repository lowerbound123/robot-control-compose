import { describe, expect, it, vi } from 'vitest'
import { readConfig } from '../src/lib/translator'
import { requestDecision } from '../src/lib/translator'
import { discoverCapabilities } from '../src/lib/agent-capabilities'
import { fixtureGateway } from './agent-fixtures'
import { translate } from './model-transport'

const config = { llm: true, enabled: true, model: 'test', timeout_s: 1 }
const context = { state: 'paused' }

describe('compatible API translator', () => {
  it('accepts only public metadata and rejects private settings', () => {
    expect(readConfig({ enabled: false, model: '', timeout_s: 30 }).enabled).toBe(false)
    expect(() => readConfig({ ...config, endpoint: 'https://private.example/v1/chat/completions' })).toThrow()
    expect(() => readConfig({ ...config, api_key: 'private-canary' })).toThrow()
    expect(() => readConfig({ ...config, enabled: 'true' })).toThrow()
    expect(() => readConfig({ ...config, model: '' })).toThrow()
    expect(() => readConfig({ ...config, timeout_s: true })).toThrow()
  })
  it('sends capabilities and user text, never requires function calling', async () => {
    const fetcher = vi.fn(async (url: string, init: RequestInit) => {
      expect(url).toBe('/ai/chat/completions')
      expect(init.mode).toBe('same-origin')
      expect(init.headers).toEqual({ 'Content-Type': 'application/json' })
      const body = JSON.parse(init.body as string)
      expect(body.tools).toBeUndefined()
      expect(body.messages.at(-1).content).toBe('复位')
      expect(body.messages[0].content).toContain('reset')
      expect(body.messages[0].content).toContain('example')
      return new Response(JSON.stringify({ choices: [{ message: { content: '{"message":"复位","tool_call":{"name":"command","arguments":{"line":"reset"}}}' } }] }))
    })
    const result = await translate(config, '复位', context, ['example'], undefined, fetcher)
    expect(result.tool_call).toEqual({ name: 'command', arguments: { line: 'reset' } })
  })
  it('treats a valid message without tool_call as a final answer', async () => {
    const content = JSON.stringify({ message: '控制权已归还。' })
    const result = await translate(config, 'x', context, [], undefined, async () => new Response(JSON.stringify({ choices: [{ message: { content } }] })))
    expect(result).toEqual({ message: '控制权已归还。', tool_call: null })
  })
  it('continues to reject invalid decisions even when tool_call is absent', async () => {
    for (const content of ['{"message":""}', '{"message":"x","name":"take"}', '{"message":"x","tool_call":false}']) {
      await expect(translate(config, 'x', context, [], undefined, async () => new Response(JSON.stringify({ choices: [{ message: { content } }] })))).rejects.toThrow()
    }
  })
  it('accepts the two single-call DSML forms observed in the session log', async () => {
    const responses = [
      ['<｜DSML｜ calls>\n<｜DSML｜ invoke name="status">\n\n</｜DSML｜ invoke>\n</｜DSML｜ calls>', { name: 'status', arguments: {} }],
      ['<｜DSML｜ calls>\n<｜DSML｜ invoke name="tool_call">\n<｜DSML｜ parameter name="name" string="true">show</｜DSML｜ parameter>\n<｜DSML｜ parameter name="arguments" string="false">{"scope": "policy"}</｜DSML｜ parameter>\n</｜DSML｜ invoke>\n</｜DSML｜ calls>', { name: 'show', arguments: { scope: 'policy' } }],
      ['<｜DSML｜ calls><｜DSML｜ invoke name="show"><｜DSML｜ parameter name="scope" string="true">policy</｜DSML｜ parameter></｜DSML｜ invoke></｜DSML｜ calls>', { name: 'show', arguments: { scope: 'policy' } }],
    ] as const
    for (const [content, call] of responses) {
      const result = await translate(config, 'x', context, [], undefined, async () => new Response(JSON.stringify({ choices: [{ message: { content } }] })))
      expect(result.tool_call).toEqual(call)
      expect(result.message).toBeTruthy()
    }
  })
  it('rejects ambiguous, malformed or unauthorized DSML calls', async () => {
    const invoke = (name: string, body = '') => `<｜DSML｜ invoke name="${name}">${body}</｜DSML｜ invoke>`
    const wrap = (body: string) => `<｜DSML｜ calls>${body}</｜DSML｜ calls>`
    const param = (name: string, value: string, type = 'true') => `<｜DSML｜ parameter name="${name}" string="${type}">${value}</｜DSML｜ parameter>`
    for (const content of [
      wrap(invoke('status') + invoke('take')),
      wrap(invoke('missing')),
      wrap(invoke('tui_command', param('text', 'resume'))),
      wrap(invoke('show', param('scope', 'invalid'))),
      wrap(invoke('show', param('scope', 'policy') + param('scope', 'rc'))),
      wrap(invoke('tool_call', param('name', 'status') + param('arguments', 'bad json', 'false'))),
      'extra' + wrap(invoke('status')),
      wrap(invoke('status')) + 'extra',
    ]) {
      await expect(translate(config, 'x', context, [], undefined, async () => new Response(JSON.stringify({ choices: [{ message: { content } }] })))).rejects.toThrow()
    }
  })
  it('does not allow a DSML tool call during the final-only round', async () => {
    const snapshot = await discoverCapabilities(fixtureGateway())
    const content = '<｜DSML｜ calls><｜DSML｜ invoke name="status"></｜DSML｜ invoke></｜DSML｜ calls>'
    await expect(requestDecision(config, { input: 'x', snapshot, history: [], exchanges: [], finalOnly: true }, {
      fetcher: async () => new Response(JSON.stringify({ choices: [{ message: { content } }] })),
    })).rejects.toThrow(/上限/)
  })
  it('fails closed on malformed JSON, unknown commands, HTTP or proxy failures', async () => {
    for (const content of ['not json', '{"message":"x","tool_call":{"name":"missing","arguments":{}}}']) {
      await expect(translate(config, 'x', context, [], undefined, async () => new Response(JSON.stringify({ choices: [{ message: { content } }] })))).rejects.toThrow()
    }
    await expect(translate(config, 'x', context, [], undefined, async () => new Response('error', { status: 401 }))).rejects.toThrow(/401/)
    await expect(translate(config, 'x', context, [], undefined, async () => { throw TypeError('Failed to fetch') })).rejects.toThrow(/代理|连接/)
  })
  it('bounds an unresponsive HTTP request', async () => {
    await expect(translate({ ...config, timeout_s: .02 }, 'x', context, [], undefined, async (_url, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
    }))).rejects.toThrow(/超时/)
  })
  it('defaults old public configs to LLM mode and refuses translation in direct mode', async () => {
    expect(readConfig({ enabled: false, model: '', timeout_s: 30 }).llm).toBe(true)
    const direct = readConfig({ llm: false, enabled: false, model: '', timeout_s: 30 })
    const fetcher = vi.fn()
    await expect(translate(direct, 'status', {}, [], undefined, fetcher)).rejects.toThrow()
    expect(fetcher).not.toHaveBeenCalled()
  })
})
