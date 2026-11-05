// Vite-only adapter. Production requests are handled by Nginx.
import { readFile } from 'node:fs/promises'
import { once } from 'node:events'
import configuration from './server-config.mjs'

function reply(response, status, value) {
  if (response.destroyed || response.writableEnded) return
  if (response.headersSent) { response.destroy(); return }
  response.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' })
  response.end(JSON.stringify(value))
}

export default function createAiMiddleware(configPath) {
  return (request, response, next) => {
    let path
    try { path = decodeURIComponent(new URL(request.url, 'http://localhost').pathname) }
    catch { reply(response, 400, { error: 'Invalid request' }); return }
    if (path !== '/config.json' && !path.startsWith('/ai/')) { next(); return }
    void (async () => {
      const host = request.headers.host || ''
      if (!/^(localhost|127\.0\.0\.1)(:[0-9]+)?$/i.test(host)) {
        reply(response, 421, { error: 'Invalid host' }); return
      }
      if (path.startsWith('/ai/')) {
        if (path !== '/ai/chat/completions') { reply(response, 404, { error: 'Not found' }); return }
        if (request.method !== 'POST') { reply(response, 405, { error: 'POST required' }); return }
        if (request.headers.origin && request.headers.origin !== `http://${host}`) {
          reply(response, 403, { error: 'Same-origin request required' }); return
        }
        if (!/^application\/json(;.*)?$/i.test(request.headers['content-type'] || '')) {
          reply(response, 415, { error: 'JSON required' }); return
        }
      }
      let config
      try {
        const content = await readFile(configPath, 'utf8')
        if (content.length > 65536) throw Error('oversized config')
        config = configuration.parseConfig(JSON.parse(content))
      } catch { reply(response, 503, { error: 'Invalid or missing WebUI server API configuration' }); return }
      if (path === '/config.json') { reply(response, 200, configuration.publicConfig(config)); return }
      if (!config.enabled) { reply(response, 503, { error: 'AI is not configured' }); return }
      if (Number(request.headers['content-length']) > 256 * 1024) {
        request.resume(); reply(response, 413, { error: 'Request too large' }); return
      }
      const chunks = []
      let bytes = 0
      for await (const chunk of request) {
        bytes += chunk.length
        if (bytes > 256 * 1024) { reply(response, 413, { error: 'Request too large' }); return }
        chunks.push(chunk)
      }
      const controller = new AbortController()
      const close = () => { if (!response.writableEnded) controller.abort() }
      response.once('close', close)
      let timedOut = false
      const timer = setTimeout(() => { timedOut = true; controller.abort() }, config.timeout_s * 1000)
      try {
        const upstream = await fetch(config.endpoint, {
          method: 'POST', redirect: 'manual', signal: controller.signal,
          headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream, application/json', 'Accept-Encoding': 'identity',
            ...(config.api_key ? { Authorization: `Bearer ${config.api_key}` } : {}) },
          body: Buffer.concat(chunks),
        })
        if (!upstream.ok) {
          await upstream.body?.cancel()
          reply(response, upstream.status >= 300 && upstream.status < 400 ? 502 : upstream.status,
            { error: 'AI proxy request failed' })
          return
        }
        const reader = upstream.body?.getReader()
        const streaming = upstream.headers.get('content-type')?.toLowerCase().includes('text/event-stream') === true
        const output = []
        let size = 0
        if (streaming) {
          response.writeHead(upstream.status, { 'Content-Type': 'text/event-stream; charset=utf-8', 'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no' })
          response.flushHeaders()
        }
        if (reader) for (;;) {
          const { value, done } = await reader.read()
          if (done) break
          size += value.length
          if (size > (streaming ? 8 * 1024 * 1024 : 128_000)) { await reader.cancel(); reply(response, 502, { error: 'AI response too large' }); return }
          if (streaming) {
            if (!response.write(value)) await once(response, 'drain', { signal: controller.signal })
          } else output.push(value)
        }
        if (!response.destroyed) {
          if (!streaming) response.writeHead(upstream.status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' })
          response.end(streaming ? undefined : Buffer.concat(output))
        }
      } catch { reply(response, timedOut ? 504 : 502, { error: 'AI proxy request failed' }) }
      finally { clearTimeout(timer); controller.abort(); response.removeListener('close', close) }
    })().catch(() => reply(response, 502, { error: 'AI proxy request failed' }))
  }
}
