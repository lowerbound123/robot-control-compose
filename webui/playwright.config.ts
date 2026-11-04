import { defineConfig } from '@playwright/test'
export default defineConfig({
  testDir: './tests/e2e',
  workers: 1,
  timeout: 30_000,
  use: { baseURL: process.env.RC_WEBUI_E2E_URL || 'http://127.0.0.1:5173', viewport: { width: 1440, height: 900 }, trace: 'retain-on-failure' },
  webServer: process.env.RC_WEBUI_E2E_URL ? undefined : [
    { command: 'python3 ../tests/webui_harness.py', url: 'http://127.0.0.1:18766/health', reuseExistingServer: !process.env.CI, timeout: 30_000, gracefulShutdown: { signal: 'SIGTERM', timeout: 20_000 } },
    { command: 'npm run dev', url: 'http://127.0.0.1:5173', reuseExistingServer: !process.env.CI,
      env: { RC_WEBUI_UPSTREAM: 'http://127.0.0.1:18765' } },
  ],
})
