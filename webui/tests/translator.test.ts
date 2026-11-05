import { describe, expect, it, vi } from 'vitest'
import { readConfig } from '../src/lib/translator'
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
