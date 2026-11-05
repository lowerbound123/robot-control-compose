import { existsSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const path = new URL('../server-config.mjs', import.meta.url)
async function serverModule() {
  expect(existsSync(path), 'server-only configuration module is missing').toBe(true)
  return (await import(/* @vite-ignore */ path.href)).default
}
const privateConfig = { endpoint: 'https://models.example:8443/prefix/v1/chat/completions?version=1',
  model: 'test-model', api_key: 'private-canary', timeout_s: 30 }

describe('server-only API configuration', () => {
  it('exports only enabled/model/timeout and keeps credentials private', async () => {
    const implementation = await serverModule()
    const parsed = implementation.parseConfig(privateConfig)
    expect(implementation.publicConfig(parsed)).toEqual({ llm: true, enabled: true, model: 'test-model', timeout_s: 30, effort: null })
    expect(parsed.host).toBe('models.example:8443')
    expect(parsed.tls_name).toBe('models.example')
    const nginx = implementation.nginxLocation(parsed)
    expect(nginx).toContain('proxy_pass $ai_endpoint;')
    expect(nginx).toContain('proxy_ssl_verify_depth 4;')
    expect(nginx).toContain('Bearer private-canary')
  })

  it('keeps monitoring available when API settings are empty', async () => {
    const implementation = await serverModule()
    const parsed = implementation.parseConfig({ endpoint: '', model: '', api_key: '', timeout_s: 30 })
    expect(implementation.publicConfig(parsed)).toEqual({ llm: true, enabled: false, model: '', timeout_s: 30, effort: null })
    expect(implementation.nginxLocation(parsed)).toContain('return 503;')
  })

  it.each([
    { endpoint: 'file:///tmp/secret' }, { endpoint: 'https://user:pass@example.com/v1' },
    { endpoint: 'https://@example.com/v1' }, { endpoint: 'https://example.com/v1#fragment' },
    { endpoint: 'https://example.com:99999/v1' }, { endpoint: 'https://example.com/";return 200;#' },
    { api_key: 'private-canary\r\nInjected: true' }, { timeout_s: 0 }, { timeout_s: 121 },
    { timeout_s: true }, { arbitrary: 'private-canary' }, { model: 123 },
  ])('rejects invalid settings without including values in errors (%j)', async patch => {
    const implementation = await serverModule()
    try {
      implementation.parseConfig({ ...privateConfig, ...patch })
      throw new Error('unexpected acceptance')
    } catch (error) {
      expect(String(error)).not.toContain('unexpected acceptance')
      expect(String(error)).not.toContain('private-canary')
    }
  })

  it('encodes literal endpoint dollars so nginx cannot expand request variables', async () => {
    const implementation = await serverModule()
    const parsed = implementation.parseConfig({ ...privateConfig, endpoint: 'http://api:8000/v1?x=$host&b=1' })
    expect(parsed.host).toBe('api:8000')
    expect(implementation.nginxLocation(parsed)).toContain('x=%24host&b=1')
  })

  it('accepts direct-only config and disables the upstream even if credentials exist', async () => {
    const implementation = await serverModule()
    expect(implementation.publicConfig(implementation.parseConfig({ llm: false }))).toEqual({
      llm: false, enabled: false, model: '', timeout_s: 30, effort: null,
    })
    const parsed = implementation.parseConfig({ ...privateConfig, llm: false })
    expect(parsed.enabled).toBe(false)
    expect(implementation.nginxLocation(parsed)).not.toContain('proxy_pass')
    expect(implementation.nginxLocation(parsed)).not.toContain('private-canary')
    expect(() => implementation.parseConfig({ ...privateConfig, llm: 'false' })).toThrow()
  })
})

async function rcUpstream(value?: string, hosts = '') {
  const implementation = await serverModule()
  expect(implementation.rcUpstream).toBeTypeOf('function')
  return implementation.rcUpstream(value, hosts)
}

describe('RC proxy upstream', () => {
  it('retains Docker DNS for the default service and accepts explicit HTTP host ports', async () => {
    expect(await rcUpstream()).toBe('robot-control:4000')
    expect(await rcUpstream('http://robot-control:4000', '172.18.0.2 robot-control')).toBe('robot-control:4000')
    expect(await rcUpstream('http://rc.internal:4100')).toBe('rc.internal:4100')
    expect(await rcUpstream('http://192.0.2.10:4000')).toBe('192.0.2.10:4000')
  })

  it('uses the exact IPv4 host-gateway alias instead of Docker DNS', async () => {
    const hosts = '# 192.0.2.9 host.docker.internal\n'
      + '192.0.2.8 host.docker.internal.invalid\n'
      + '::1 localhost host.docker.internal\n'
      + '192.0.2.7 unrelated # host.docker.internal\n'
      + '192.0.2.1\tgateway host.docker.internal # Docker host gateway\n'
    expect(await rcUpstream('http://HOST.DOCKER.INTERNAL:4000', hosts)).toBe('192.0.2.1:4000')
  })

  it.each(['', '127.0.0.1 localhost', '999.1.2.3 host.docker.internal',
    '::1 host.docker.internal', '192.0.2.1 host.docker.internal.invalid'])
  ('fails closed when the host gateway has no valid IPv4 mapping (%j)', async hosts => {
    await expect(rcUpstream('http://host.docker.internal:4000', hosts))
      .rejects.toThrow('Invalid WebUI RC upstream')
  })

  it.each(['', 'https://robot-control:4000', 'http://robot-control', 'http://robot-control:0',
    'http://robot-control:65536', 'http://robot-control:4000/', 'http://robot-control:4000/mcp',
    'http://user:private-canary@robot-control:4000', 'http://robot-control:4000?private-canary',
    'http://robot-control:4000#private-canary', 'http://robot-control:4000;private-canary',
    'http://$host:4000', 'http://robot-control:4000\n', 'http://robot-control:4000\\',
    'http://-robot-control:4000', 'http://rc..internal:4000', 'http://999.1.2.3:4000'])
  ('rejects invalid upstreams without echoing their values (%j)', async value => {
    await expect(rcUpstream(value)).rejects.toThrow('Invalid WebUI RC upstream')
    await expect(rcUpstream(value)).rejects.not.toThrow('private-canary')
  })
})

