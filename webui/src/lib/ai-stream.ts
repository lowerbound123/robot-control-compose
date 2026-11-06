import { createParser } from 'eventsource-parser'
import { audit, type AuditSink } from './session-log'

export type TranslationProgress = { kind: 'reasoning' | 'message'; text: string }
export const MAX_STREAM_BYTES = 8 * 1024 * 1024
const MAX_CONTENT = 16_000
const MAX_REASONING = 32_000
type ObjectValue = Record<string, unknown>
const object = (value: unknown): ObjectValue => value && typeof value === 'object' && !Array.isArray(value) ? value as ObjectValue : {}

// This is display-only. Partial JSON is never returned to the command executor.
function stringAt(source: string, start: number) {
  let text = '', index = start + 1
  for (; index < source.length; index++) {
    const char = source[index]!
    if (char === '"') return { text, end: index + 1, complete: true }
    if (char !== '\\') { text += char; continue }
    const escaped = source[++index]
    if (escaped === undefined) break
    if (escaped === 'u') {
      const digits = source.slice(index + 1, index + 5)
      if (!/^[0-9a-f]{4}$/i.test(digits)) break
      text += String.fromCharCode(parseInt(digits, 16))
      index += 4
    } else {
      const escapes: Record<string, string> = { '"': '"', '\\': '\\', '/': '/', b: '\b', f: '\f', n: '\n', r: '\r', t: '\t' }
      if (!(escaped in escapes)) break
      text += escapes[escaped]
    }
  }
  return { text: text.replace(/[\uD800-\uDBFF]$/, ''), end: index, complete: false }
}

function messagePreview(source: string): string | undefined {
  let depth = 0
  for (let i = 0; i < source.length; i++) {
    const char = source[i]
    if (char === '{' || char === '[') depth++
    else if (char === '}' || char === ']') depth--
    else if (char === '"') {
      const value = stringAt(source, i)
      if (!value.complete) return undefined
      let next = value.end
      while (/\s/.test(source[next] || '') && next < source.length) next++
      if (depth === 1 && value.text === 'message' && source[next] === ':') {
        next++
        while (/\s/.test(source[next] || '') && next < source.length) next++
        return source[next] === '"' ? stringAt(source, next).text : undefined
      }
      i = value.end - 1
    }
  }
}

function checkChoice(choice: ObjectValue, delta: ObjectValue) {
  if (choice.finish_reason != null && choice.finish_reason !== 'stop') throw Error('AI 输出未正常完成，未执行任何指令。')
  if (delta.refusal || delta.tool_calls || delta.function_call) throw Error('AI 未返回可执行的指令说明。')
}

export async function readAiResponse(response: Response, signal: AbortSignal, onProgress?: (value: TranslationProgress) => void, observer?: AuditSink) {
  const streaming = response.headers.get('content-type')?.toLowerCase().includes('text/event-stream') === true
  const reader = response.body?.getReader()
  if (!reader) throw Error('AI 响应为空，未执行任何指令。')
  const decoder = new TextDecoder('utf-8', { fatal: true })
  let bytes = 0, content = '', reasoning = '', preview = '', json = '', done = false
  const notify = (kind: TranslationProgress['kind'], text: string) => { if (!signal.aborted) onProgress?.({ kind, text }) }
  const append = (delta: ObjectValue) => {
    const thinking = typeof delta.reasoning_content === 'string' ? delta.reasoning_content : delta.reasoning
    if (typeof thinking === 'string' && thinking) {
      reasoning += thinking
      if (reasoning.length > MAX_REASONING) throw Error('AI 思考内容过长，未执行任何指令。')
      notify('reasoning', reasoning)
    }
    if (delta.content != null && typeof delta.content !== 'string') throw Error('AI 响应缺少有效的 message.content。')
    if (typeof delta.content === 'string') {
      content += delta.content
      if (content.length > MAX_CONTENT) throw Error('AI 响应正文过长，未执行任何指令。')
      const next = messagePreview(content)
      if (next !== undefined && next !== preview) { preview = next; notify('message', preview) }
    }
  }
  const parser = createParser({
    maxBufferSize: MAX_STREAM_BYTES,
    onEvent(event) {
      if (done) return
      if (event.data.trim() === '[DONE]') { done = true; return }
      let value: ObjectValue
      try { value = object(JSON.parse(event.data)) } catch { throw Error('AI 流式响应格式错误，未执行任何指令。') }
      if (value.error || event.event === 'error') throw Error('AI 流式响应失败，未执行任何指令。')
      if (!Array.isArray(value.choices)) throw Error('AI 流式响应缺少 choices。')
      if (!value.choices.length) return // Optional usage-only chunk.
      const selected = value.choices.find(item => object(item).index === 0)
        ?? value.choices.find(item => object(item).index == null)
      if (!selected) return
      const choice = object(selected)
      const delta = object(choice.delta)
      checkChoice(choice, delta)
      append(delta)
    },
    onError(error) { throw Error(error.type === 'max-buffer-size-exceeded' ? 'AI 响应过大，未执行任何指令。' : 'AI 流式响应格式错误，未执行任何指令。') },
  })
  const cancel = () => { void reader.cancel().catch(() => {}) }
  signal.addEventListener('abort', cancel, { once: true })
  try {
    if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
    for (;;) {
      const chunk = await reader.read()
      if (signal.aborted) throw new DOMException('Aborted', 'AbortError')
      if (chunk.done) break
      bytes += chunk.value.byteLength
      if (bytes > (streaming ? MAX_STREAM_BYTES : 128_000)) throw Error('AI 响应过大，未执行任何指令。')
      const text = decoder.decode(chunk.value, { stream: true })
      if (text) audit(observer, 'model.response.chunk', { text })
      if (streaming) parser.feed(text)
      else json += text
      if (done) break
    }
    if (streaming) {
      if (!done) throw Error('AI 输出连接提前结束，未执行任何指令。')
    } else {
      json += decoder.decode()
      let body: ObjectValue
      try { body = object(JSON.parse(json)) } catch { throw Error('AI API 未返回有效 JSON。') }
      const choice = object(Array.isArray(body.choices) ? body.choices[0] : undefined)
      const message = object(choice.message)
      checkChoice(choice, message)
      append(message)
    }
    if (!content) throw Error('AI 响应缺少有效的 message.content。')
    audit(observer, 'model.response.completed', { content, reasoning, done: streaming ? done : true })
    return content
  } finally {
    signal.removeEventListener('abort', cancel)
    await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
}
