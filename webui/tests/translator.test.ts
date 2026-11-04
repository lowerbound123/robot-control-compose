import { describe, expect, it, vi } from 'vitest'
import { readConfig, translate } from '../src/lib/translator'

const config = { endpoint: 'http://127.0.0.1:9000/v1/chat/completions', model: 'test', api_key: 'local-test-only', timeout_s: 1 }
const context = { state: 'paused' }

describe('compatible API translator', () => {
  it('allows empty API settings but rejects invalid endpoints and timeouts', () => {
    expect(readConfig({ endpoint: '', model: '', api_key: '', timeout_s: 30 }).endpoint).toBe('')
    expect(() => readConfig({ ...config, endpoint: 'file:///tmp/key' })).toThrow()
    expect(() => readConfig({ ...config, timeout_s: true })).toThrow()
  })
  it('sends capabilities and user text, never requires function calling', async () => {
    const fetcher = vi.fn(async (_url: string, init: RequestInit) => {
      const body = JSON.parse(init.body as string)
      expect(body.tools).toBeUndefined()
      expect(body.messages.at(-1).content).toBe('复位')
      expect(body.messages[0].content).toContain('reset')
      expect(body.messages[0].content).toContain('example')
      return new Response(JSON.stringify({ choices: [{ message: { content: '{"message":"复位","steps":[{"action":"reset"}]}' } }] }))
    })
    const result = await translate(config, '复位', context, ['example'], undefined, fetcher)
    expect(result.steps).toEqual([{ action: 'reset' }])
  })
  it('fails closed on malformed JSON, unknown commands, HTTP or CORS failures', async () => {
    for (const content of ['not json', '{"message":"x","steps":[{"action":"exit"}]}']) {
      await expect(translate(config, 'x', context, [], undefined, async () => new Response(JSON.stringify({ choices: [{ message: { content } }] })))).rejects.toThrow()
    }
    await expect(translate(config, 'x', context, [], undefined, async () => new Response('error', { status: 401 }))).rejects.toThrow(/401/)
    await expect(translate(config, 'x', context, [], undefined, async () => { throw TypeError('Failed to fetch') })).rejects.toThrow(/CORS|跨域|连接/)
  })
  it('bounds an unresponsive HTTP request', async () => {
    await expect(translate({ ...config, timeout_s: .02 }, 'x', context, [], undefined, async (_url, init) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
    }))).rejects.toThrow(/超时/)
  })
})
