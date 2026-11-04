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
    expect(implementation.publicConfig(parsed)).toEqual({ llm: true, enabled: true, model: 'test-model', timeout_s: 30 })
    expect(parsed.host).toBe('models.example:8443')
    expect(parsed.tls_name).toBe('models.example')
    const nginx = implementation.nginxLocation(parsed)
    expect(nginx).toContain('proxy_pass $ai_endpoint;')
    expect(nginx).toContain('Bearer private-canary')
  })

  it('keeps monitoring available when API settings are empty', async () => {
    const implementation = await serverModule()
    const parsed = implementation.parseConfig({ endpoint: '', model: '', api_key: '', timeout_s: 30 })
    expect(implementation.publicConfig(parsed)).toEqual({ llm: true, enabled: false, model: '', timeout_s: 30 })
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
      llm: false, enabled: false, model: '', timeout_s: 30,
    })
    const parsed = implementation.parseConfig({ ...privateConfig, llm: false })
    expect(parsed.enabled).toBe(false)
    expect(implementation.nginxLocation(parsed)).not.toContain('proxy_pass')
    expect(implementation.nginxLocation(parsed)).not.toContain('private-canary')
    expect(() => implementation.parseConfig({ ...privateConfig, llm: 'false' })).toThrow()
  })
})

