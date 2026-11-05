import { expect, it, vi } from 'vitest'
import { translate } from './model-transport'

const config = { llm: true, enabled: true, model: 'test', timeout_s: 2 }
const content = JSON.stringify({ message: '准备复位', tool_call: { name: 'command', arguments: { line: 'reset' } } })
const event = (delta: object, finish_reason: string | null = null) => `data: ${JSON.stringify({ choices: [{ index: 0, delta, finish_reason }] })}\r\n\r\n`
const ending = event({}, 'stop') + 'data: [DONE]\r\n\r\n'
function response(text: string, bytewise = false) {
  const bytes = new TextEncoder().encode(text)
  return new Response(new ReadableStream({ start(c) {
    if (bytewise) for (const byte of bytes) c.enqueue(Uint8Array.of(byte))
    else c.enqueue(bytes)
    c.close()
  } }), { headers: { 'Content-Type': 'text/event-stream; charset=utf-8' } })
}
function run(text: string, progress = vi.fn(), bytewise = false) {
  return translate(config, '复位', {}, [], undefined, async () => response(text, bytewise), progress)
}

it('requests streaming and displays reasoning and message before DONE without resolving the plan', async () => {
  let stream!: ReadableStreamDefaultController<Uint8Array>
  const progress = vi.fn(), settled = vi.fn()
  const result = translate(config, '复位', {}, [], undefined, async (_url, init) => {
    expect(JSON.parse(init.body as string).stream).toBe(true)
    return new Response(new ReadableStream({ start(c) { stream = c } }), { headers: { 'Content-Type': 'text/event-stream' } })
  }, progress)
  void result.then(settled)
  await vi.waitFor(() => expect(stream).toBeDefined())
  const push = (text: string) => stream.enqueue(new TextEncoder().encode(text))
  push(event({ reasoning_content: '检查当前状态' }) + event({ content: '{"message":"准备' }))
  await vi.waitFor(() => expect(progress).toHaveBeenCalledWith({ kind: 'message', text: '准备' }))
  expect(progress).toHaveBeenCalledWith({ kind: 'reasoning', text: '检查当前状态' })
  expect(settled).not.toHaveBeenCalled()
  push(event({ content: '复位","tool_call":{"name":"command","arguments":{"line":"reset"}}}' }) + ending)
  stream.close()
  expect((await result).tool_call).toEqual({ name: 'command', arguments: { line: 'reset' } })
})

it('decodes split UTF-8, CRLF, escaped strings and top-level message after steps', async () => {
  const progress = vi.fn()
  const message = '中文 "引号"\n反斜线\\ 和😀'
  const json = JSON.stringify({ tool_call: null, message }).replace('😀', '\\ud83d\\ude00')
  const text = '\ufeff: heartbeat\r\n\r\n' + [...json].map(content => event({ content })).join('') + ending
  expect((await run(text, progress, true)).message).toBe(message)
  expect(progress.mock.calls.filter(([p]) => p.kind === 'message').at(-1)?.[0].text).toBe(message)
  expect(progress.mock.calls.every(([p]) => !p.text.includes('\ufffd'))).toBe(true)
})

it('ignores role, usage and heartbeat packets and supports the reasoning alias', async () => {
  const progress = vi.fn()
  const text = event({ role: 'assistant' }) + ': ping\n\n' + event({ reasoning: '先确认' })
    + event({ content }) + 'data: {"choices":[],"usage":{"total_tokens":20}}\n\n' + ending
  expect((await run(text, progress)).message).toBe('准备复位')
  expect(progress).toHaveBeenCalledWith({ kind: 'reasoning', text: '先确认' })
})

it.each([
  [event({ content }), 'missing DONE'],
  [event({ content }, 'length') + 'data: [DONE]\n\n', 'truncation'],
  [event({ content }, 'content_filter') + 'data: [DONE]\n\n', 'filter'],
  [event({ content, refusal: 'no' }) + ending, 'refusal'],
  ['data: {broken}\n\n' + ending, 'bad event'],
  ['data: {"error":{"message":"private-provider-error"}}\n\n' + ending, 'provider error'],
  [event({ content: '{"message":"好","tool_call":{"name":"missing","arguments":{}}}' }) + ending, 'invalid plan'],
])('never returns a plan after an invalid stream (%s, %s)', async text => {
  await expect(run(text)).rejects.toThrow()
})

it.each([
  event({ content: 'x'.repeat(16_001) }) + ending,
  event({ reasoning_content: 'x'.repeat(32_001) }) + ending,
  ':' + 'x'.repeat(8 * 1024 * 1024) + '\n\n' + ending,
])('bounds decoded and wire content', async text => { await expect(run(text)).rejects.toThrow(/过大|过长/) })

it('cancels a stalled body and does not emit late progress', async () => {
  const abort = new AbortController(), progress = vi.fn(), cancelled = vi.fn()
  const result = translate(config, 'x', {}, [], abort.signal, async () => new Response(new ReadableStream({ cancel: cancelled }),
    { headers: { 'Content-Type': 'text/event-stream' } }), progress)
  await Promise.resolve()
  abort.abort()
  await expect(result).rejects.toThrow(/取消/)
  expect(cancelled).toHaveBeenCalled()
  expect(progress).not.toHaveBeenCalled()
})

it('times out a stalled stream even after response headers arrived', async () => {
  await expect(translate({ ...config, timeout_s: .02 }, 'x', {}, [], undefined,
    async () => new Response(new ReadableStream(), { headers: { 'Content-Type': 'text/event-stream' } }))).rejects.toThrow(/超时/)
})

it('keeps JSON compatibility and emits its real reasoning and final message', async () => {
  const progress = vi.fn()
  await translate(config, 'x', {}, [], undefined, async () => new Response(JSON.stringify({ choices: [{
    message: { content, reasoning_content: '检查状态' }, finish_reason: 'stop',
  }] }), { headers: { 'Content-Type': 'application/json' } }), progress)
  expect(progress.mock.calls.map(([p]) => p)).toEqual([{ kind: 'reasoning', text: '检查状态' }, { kind: 'message', text: '准备复位' }])
})

it('never mixes a different choice into the selected command stream', async () => {
  const other = `data: ${JSON.stringify({ choices: [{ index: 1, delta: { content: 'wrong choice' } }] })}\n\n`
  expect((await run(other + event({ content }) + ending)).message).toBe('准备复位')
})
