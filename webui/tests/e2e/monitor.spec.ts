import { expect, test, type Page } from '@playwright/test'
const api = process.env.RC_WEBUI_TEST_API || 'http://127.0.0.1:18766'
const rc = process.env.RC_WEBUI_E2E_URL || 'http://127.0.0.1:18765'
async function configure(page: Page, options = {}) {
  await page.route('**/config.json', route => route.fulfill({ json: {
    enabled: true, model: 'test-translator', timeout_s: 3, ...options,
  } }))
}
test.beforeEach(async ({ request }) => { expect((await request.post(`${api}/test/reset`)).ok()).toBeTruthy() })

test('route, real images, reset, and mobile layout work without an AI configuration', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await configure(page, { enabled: false, model: '' })
  await page.goto('/')
  await expect(page).toHaveURL(/\/monitor$/)
  await expect(page).toHaveTitle('Robot Control · 监控')
  for (const camera of ['left_wrist', 'head', 'right_wrist']) {
    await expect.poll(() => page.getByTestId(`camera-${camera}`).evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(640)
  }
  await page.getByRole('button', { name: '复位', exact: true }).click()
  await expect(page.getByRole('status')).toContainText('复位指令已完成')
  await expect(page.getByRole('button', { name: '复位', exact: true })).toBeEnabled()
  await page.reload()
  await expect(page.getByRole('tab', { name: '监控' })).toBeVisible()
  await page.setViewportSize({ width: 390, height: 844 })
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  const input = page.getByLabel('自然语言指令')
  await expect.poll(() => input.evaluate(element => element.getBoundingClientRect().bottom <= innerHeight)).toBe(true)
  await page.getByTestId('camera-right_wrist').scrollIntoViewIfNeeded()
  await expect(page.getByTestId('camera-right_wrist')).toBeInViewport()
  await expect(input).toBeInViewport()
  await expect(page.getByRole('tab', { name: '监控' })).toBeInViewport()
  expect(errors).toEqual([])
})

test('browser AI translation starts recording without takeover and renders receipts', async ({ page, request }) => {
  await configure(page)
  await page.goto('/monitor')
  const before = await (await request.get(`${rc}/health`)).json()
  await page.getByLabel('自然语言指令').fill('开始录制')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect(page.getByRole('status')).toContainText('已执行')
  const after = await (await request.get(`${rc}/health`)).json()
  expect(after.recording.state).toBe('recording')
  expect(after.control_owner).toBe(before.control_owner)
  expect(after.generation).toBe(before.generation)
  await page.getByRole('button', { name: '展开操作回执' }).click()
  await expect(page.getByLabel('操作回执', { exact: true })).toContainText('operation_id')
  expect((await (await request.get(`${api}/test/requests`)).json()).length).toBe(1)
})

test('stale camera status recovers and closing the page releases all preview subscriptions', async ({ page, request }) => {
  await configure(page, { enabled: false, model: '' })
  await page.goto('/monitor')
  const head = page.locator('[data-camera="head"]')
  await expect(head.locator('.camera-state')).toHaveAttribute('data-state', 'live')
  // Backend stale-source/recovery behavior is covered by test_web_preview.py;
  // this exercises how the browser renders that status without losing the image.
  await page.route('**/preview/status', async route => {
    const response = await route.fetch()
    const json = await response.json()
    json.cameras.head.state = 'stale'
    json.cameras.head.frame_age_s = 3.2
    await route.fulfill({ response, json })
  })
  await expect(head).toContainText('画面已过期')
  await expect(head).toContainText('3.2 秒前')
  await expect(head.getByTestId('camera-head')).toBeVisible()
  await page.unroute('**/preview/status')
  await expect(head.locator('.camera-state')).toHaveAttribute('data-state', 'live')
  await page.close()
  await expect.poll(async () => {
    const status = await (await request.get(`${rc}/preview/status`)).json()
    return Object.values(status.cameras).every((camera: any) => camera.subscribers === 0)
  }).toBe(true)
})

test('invalid and ambiguous translations never partially execute', async ({ page, request }) => {
  await configure(page)
  await page.goto('/monitor')
  const before = await (await request.get(`${rc}/health`)).json()
  for (const input of ['非法指令', '未知策略', '坏JSON', 'HTTP错误']) {
    await page.getByLabel('自然语言指令').fill(input)
    await page.getByRole('button', { name: '发送指令' }).click()
    await expect(page.getByRole('status')).toHaveAttribute('data-error', 'true')
    await expect(page.getByRole('button', { name: '发送指令' })).toBeVisible()
  }
  const after = await (await request.get(`${rc}/health`)).json()
  expect(after.generation).toBe(before.generation)
  expect(after.control_owner).toBe(before.control_owner)
})

test('manual intervention while the model is thinking discards the old plan', async ({ page, request }) => {
  await configure(page)
  await page.goto('/monitor')
  await page.getByLabel('自然语言指令').fill('慢慢复位')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect.poll(async () => (await (await request.get(`${api}/test/requests`)).json()).length).toBe(1)
  const paused = await (await request.post(`${api}/test/pause`)).json()
  await expect(page.getByRole('status')).toContainText('后续步骤已停止')
  const after = await (await request.get(`${rc}/health`)).json()
  expect(after.generation).toBe(paused.result.generation)
  expect(after.control_owner).toBe('policy')
})

test('API timeout and proxy connection failures keep reset usable', async ({ page }) => {
  await configure(page, { timeout_s: .05 })
  await page.goto('/monitor')
  await page.getByLabel('自然语言指令').fill('超时')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect(page.getByRole('status')).toContainText('超时')
  await page.unroute('**/config.json')
  await configure(page)
  await page.route('**/ai/chat/completions', route => route.abort('failed'))
  await page.reload()
  await page.getByLabel('自然语言指令').fill('查询状态')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect(page.getByRole('status')).toContainText('同源 AI 代理')
  await expect(page.getByRole('button', { name: '复位', exact: true })).toBeEnabled()
})

test('real same-origin AI works without CORS and keeps endpoint/key out of the browser', async ({ page, request }, testInfo) => {
  const errors: string[] = []
  const sent: { url: string; authorization?: string }[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', request => { if (request.url().includes('chat/completions') || request.url().includes('/no-cors')) {
    sent.push({ url: request.url(), authorization: request.headers().authorization })
  } })
  const preflight = await request.fetch(`${api}/no-cors`, { method: 'OPTIONS', headers: { Origin: 'http://127.0.0.1:5173' } })
  expect(preflight.status()).toBe(403)
  expect(preflight.headers()['access-control-allow-origin']).toBeUndefined()
  await page.goto('/monitor')
  const exposed = await page.evaluate(async () => (await fetch('/config.json?raw')).text())
  expect(Object.keys(JSON.parse(exposed)).sort()).toEqual(['enabled', 'llm', 'model', 'timeout_s'])
  expect(exposed).not.toContain('local-test-only')
  expect(exposed).not.toContain('endpoint')
  if (process.env.RC_WEBUI_TEST_PRIVATE_CONFIG && !process.env.RC_WEBUI_E2E_URL) {
    const denied = await request.get(`/@fs${process.env.RC_WEBUI_TEST_PRIVATE_CONFIG}`)
    expect(denied.status()).toBe(403)
    expect(await denied.text()).not.toContain('local-test-only')
  }
  await page.getByLabel('自然语言指令').fill('查询状态')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect(page.getByRole('status')).toContainText('已执行')
  expect(sent).toHaveLength(1)
  expect(new URL(sent[0]!.url).origin).toBe(new URL(page.url()).origin)
  expect(new URL(sent[0]!.url).pathname).toBe('/ai/chat/completions')
  expect(sent[0]!.authorization).toBeUndefined()
  const received = await (await request.get(`${api}/test/requests`)).json()
  expect(received).toHaveLength(1)
  expect(received[0].auth_valid).toBe(true)
  expect(received[0].origin).toBeNull()
  expect(received[0].cookie).toBeNull()
  expect(errors).toEqual([])
  await expect.poll(() => page.getByTestId('camera-head').evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(640)
  for (const camera of ['head', 'left_wrist', 'right_wrist']) {
    await expect(page.locator(`[data-camera="${camera}"] .camera-state`)).toHaveAttribute('data-state', 'live')
  }
  await page.screenshot({ path: testInfo.outputPath('same-origin-ai.png') })
})

test('provider errors and redirects do not disclose server credentials or execute commands', async ({ page, request }) => {
  await page.goto('/monitor')
  const before = await (await request.get(`${rc}/health`)).json()
  for (const [input, code] of [['认证错误', 401], ['其他错误', 418], ['重定向', 502]] as const) {
    await page.getByLabel('自然语言指令').fill(input)
    await page.getByRole('button', { name: '发送指令' }).click()
    await expect(page.getByRole('status')).toContainText(`HTTP ${code}`)
    await expect(page.getByRole('status')).not.toContainText('local-test-only')
    const after = await (await request.get(`${rc}/health`)).json()
    expect(after.generation).toBe(before.generation)
  }
})

test('switching to example self-chain stays paused until explicit resume', async ({ page, request }) => {
  await configure(page)
  await page.goto('/monitor')
  await page.getByLabel('自然语言指令').fill('切换自链')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect(page.getByRole('status')).toContainText('已执行', { timeout: 20_000 })
  const switched = await (await request.get(`${rc}/health`)).json()
  expect(switched.policy).toBe('example_chain')
  expect(switched.state).toBe('paused')
  expect(switched.control_owner).toBe('mcp')
  await page.getByLabel('自然语言指令').fill('继续')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect(page.getByRole('status')).toContainText('继续 · 已执行')
  expect((await (await request.get(`${rc}/health`)).json()).state).toBe('running')
})
