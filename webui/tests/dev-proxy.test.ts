import { existsSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { afterEach, expect, it } from 'vitest'

const cleanup: (() => Promise<void> | void)[] = []
afterEach(async () => { for (const close of cleanup.splice(0).reverse()) await close() })

async function listen(handler: (req: IncomingMessage, res: ServerResponse) => void) {
  const server = createServer(handler)
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  cleanup.push(() => new Promise<void>(resolve => { server.closeAllConnections(); server.close(() => resolve()) }))
  return `http://127.0.0.1:${(server.address() as { port: number }).port}`
}

async function proxy(upstream: string, patch = {}) {
  const modulePath = new URL('../dev-proxy.mjs', import.meta.url)
  expect(existsSync(modulePath), 'server-only development proxy is missing').toBe(true)
  const createAiMiddleware = (await import(/* @vite-ignore */ modulePath.href)).default
  const directory = mkdtempSync(join(tmpdir(), 'rc-webui-ai-'))
  cleanup.push(() => rmSync(directory, { recursive: true }))
  const path = join(directory, 'config.json')
  writeFileSync(path, JSON.stringify({ endpoint: upstream + '/no-cors?version=1', model: 'fixture',
    api_key: 'server-private-canary', timeout_s: 1, ...patch }), { mode: 0o600 })
  const handler = createAiMiddleware(path)
  return await listen((req, res) => handler(req, res, () => { res.writeHead(404); res.end() }))
}

it('sanitizes config and proxies a no-CORS API with server-owned target and key', async () => {
  const calls: unknown[] = []
  const upstream = await listen(async (req, res) => {
    let body = ''; for await (const chunk of req) body += chunk
    calls.push({ path: req.url, auth: req.headers.authorization, origin: req.headers.origin, cookie: req.headers.cookie,
      body: JSON.parse(body) })
    res.setHeader('Content-Type', 'application/json')
    res.end('{"ok":true}')
  })
  const origin = await proxy(upstream)
  const publicConfig = await (await fetch(origin + '/config.json?raw')).text()
  expect(JSON.parse(publicConfig)).toEqual({ llm: true, enabled: true, model: 'fixture', timeout_s: 1, effort: null })
  expect(publicConfig).not.toContain(upstream)
  expect(publicConfig).not.toContain('canary')
  const response = await fetch(origin + '/ai/chat/completions?target=http://ignored.invalid', {
    method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: 'Bearer browser-key',
      Origin: origin, Cookie: 'browser-cookie=private' }, body: '{"model":"fixture"}',
  })
  expect(await response.json()).toEqual({ ok: true })
  expect(calls).toEqual([{ path: '/no-cors?version=1', auth: 'Bearer server-private-canary',
    origin: undefined, cookie: undefined, body: { model: 'fixture' } }])
})

it('blocks cross-origin requests, wrong methods and unconfigured AI before calling upstream', async () => {
  let calls = 0
  const upstream = await listen((_req, res) => { calls++; res.end('{}') })
  const origin = await proxy(upstream)
  expect((await fetch(origin + '/ai/chat/completions')).status).toBe(405)
  expect((await fetch(origin + '/ai/chat/completions', { method: 'POST',
    headers: { 'Content-Type': 'application/json', Origin: 'https://untrusted.invalid' }, body: '{}' })).status).toBe(403)
  expect((await fetch(origin + '/ai/chat/completions', { method: 'POST', body: '{}' })).status).toBe(415)
  const disabled = await proxy(upstream, { endpoint: '', model: '' })
  expect((await (await fetch(disabled + '/config.json')).json()).enabled).toBe(false)
  expect((await fetch(disabled + '/ai/chat/completions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })).status).toBe(503)
  expect(calls).toBe(0)
  const direct = await proxy(upstream, { llm: false })
  expect((await fetch(direct + '/ai/chat/completions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })).status).toBe(503)
  expect(calls).toBe(0)
})

it.each([401, 418, 429, 302, 500])('does not expose upstream error data or follow redirects (%s)', async status => {
  let calls = 0
  const upstream = await listen((_req, res) => {
    calls++; res.writeHead(status, { Location: 'https://private.example/private-canary', 'Set-Cookie': 'key=private-canary' })
    res.end('server-private-canary')
  })
  const origin = await proxy(upstream)
  const response = await fetch(origin + '/ai/chat/completions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
  expect(response.status).toBe(status === 302 ? 502 : status)
  expect(response.headers.has('location')).toBe(false)
  expect(response.headers.has('set-cookie')).toBe(false)
  expect(await response.text()).not.toContain('private-canary')
  expect(calls).toBe(1)
})

it('bounds slow requests without retries', async () => {
  let calls = 0
  const upstream = await listen((_req, _res) => { calls++ })
  const origin = await proxy(upstream, { timeout_s: .05 })
  const response = await fetch(origin + '/ai/chat/completions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
  expect(response.status).toBe(504)
  expect(calls).toBe(1)
})

it('forwards the first SSE chunk before upstream completion without exposing headers', async () => {
  let release!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  const upstream = await listen(async (_req, res) => {
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Set-Cookie': 'private-canary' })
    res.write('data: {"choices":[]}\n\n')
    await gate
    res.end('data: [DONE]\n\n')
  })
  const origin = await proxy(upstream)
  try {
    const response = await fetch(origin + '/ai/chat/completions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"stream":true}' })
    expect(response.headers.get('content-type')).toContain('text/event-stream')
    expect(response.headers.has('set-cookie')).toBe(false)
    const reader = response.body!.getReader()
    expect(new TextDecoder().decode((await reader.read()).value)).toBe('data: {"choices":[]}\n\n')
    release()
    expect(new TextDecoder().decode((await reader.read()).value)).toBe('data: [DONE]\n\n')
    expect((await reader.read()).done).toBe(true)
  } finally { release() }
})

it('terminates an already-started stream on timeout instead of appending JSON or retrying', async () => {
  let calls = 0
  const upstream = await listen((_req, res) => {
    calls++
    res.writeHead(200, { 'Content-Type': 'text/event-stream' })
    res.write(': heartbeat\n\n')
  })
  const origin = await proxy(upstream, { timeout_s: .1 })
  const response = await fetch(origin + '/ai/chat/completions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
  expect(response.status).toBe(200)
  const reader = response.body!.getReader()
  expect(new TextDecoder().decode((await reader.read()).value)).toBe(': heartbeat\n\n')
  await expect(reader.read()).rejects.toThrow()
  expect(calls).toBe(1)
})

it('cancels upstream when a streaming browser disconnects', async () => {
  let closed!: () => void
  const close = new Promise<void>(resolve => { closed = resolve })
  const upstream = await listen((_req, res) => {
    res.once('close', closed)
    res.writeHead(200, { 'Content-Type': 'text/event-stream' })
    res.write(': ready\n\n')
  })
  const origin = await proxy(upstream)
  const response = await fetch(origin + '/ai/chat/completions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
  const reader = response.body!.getReader()
  await reader.read()
  await reader.cancel()
  await close
})
