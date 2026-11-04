import { defineConfig } from '@playwright/test'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

// Never point browser tests at a developer's real provider or credentials.
let configPath = process.env.RC_WEBUI_TEST_PRIVATE_CONFIG
if (!process.env.RC_WEBUI_E2E_URL && !configPath) {
  const directory = mkdtempSync(join(tmpdir(), 'rc-webui-e2e-'))
  configPath = join(directory, 'config.json')
  writeFileSync(configPath, JSON.stringify({ endpoint: 'http://127.0.0.1:18766/no-cors',
    model: 'test-translator', api_key: 'local-test-only', timeout_s: 3 }), { mode: 0o600 })
  process.env.RC_WEBUI_TEST_PRIVATE_CONFIG = configPath
  process.once('exit', () => rmSync(directory, { recursive: true, force: true }))
}
export default defineConfig({
  testDir: './tests/e2e',
  outputDir: process.env.RC_WEBUI_E2E_ARTIFACTS || join(tmpdir(), 'rc-webui-e2e-artifacts'),
  workers: 1,
  timeout: 30_000,
  use: { baseURL: process.env.RC_WEBUI_E2E_URL || 'http://127.0.0.1:5173', viewport: { width: 1440, height: 900 }, trace: 'retain-on-failure' },
  webServer: process.env.RC_WEBUI_E2E_URL ? undefined : [
    { command: 'python3 ../tests/webui_harness.py', url: 'http://127.0.0.1:18766/health', reuseExistingServer: !process.env.CI, timeout: 30_000, gracefulShutdown: { signal: 'SIGTERM', timeout: 20_000 } },
    { command: 'npm run dev', url: 'http://127.0.0.1:5173', reuseExistingServer: false,
      env: { RC_WEBUI_UPSTREAM: 'http://127.0.0.1:18765', RC_WEBUI_CONFIG_PATH: configPath! } },
  ],
})
