import { expect, it } from 'vitest'
import configuration from '../server-config.mjs'
import { readConfig } from '../src/lib/translator'
const privateConfig = { llm: true, endpoint: 'http://localhost/v1/chat/completions', model: 'test', api_key: '', timeout_s: 30 }

it('defaults omitted effort to null and preserves every supported setting in public metadata', () => {
  expect(configuration.publicConfig(configuration.parseConfig(privateConfig)).effort).toBeNull()
  for (const effort of [null, 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max']) {
    const value = configuration.publicConfig(configuration.parseConfig({ ...privateConfig, effort }))
    expect(value.effort).toBe(effort)
    expect(readConfig(value).effort).toBe(effort)
  }
  expect(readConfig({ llm: true, enabled: true, model: 'test', timeout_s: 30 }).effort).toBeNull()
})
it('rejects invalid effort values without reflecting private fields', () => {
  for (const effort of ['', true, 2, {}, 'private-canary']) {
    expect(() => configuration.parseConfig({ ...privateConfig, effort })).toThrow()
    expect(() => readConfig({ enabled: true, model: 'test', timeout_s: 30, effort })).toThrow()
  }
})
