import { expect, test } from '@playwright/test'
const api = process.env.RC_WEBUI_TEST_API || 'http://127.0.0.1:18766'
const rc = process.env.RC_WEBUI_E2E_URL || 'http://127.0.0.1:18765'

test.beforeEach(async ({ page, request }) => {
  expect((await request.post(`${api}/test/reset`)).ok()).toBeTruthy()
  if (!process.env.RC_WEBUI_DIRECT_SERVER) await page.route('**/config.json', route => route.fulfill({ json: {
    llm: false, enabled: false, model: '', timeout_s: 30,
  } }))
})
test.afterEach(async ({ request }) => { await request.post(`${api}/test/batch-release`) })

test('direct text sends one batch without AI, take or policy discovery', async ({ page, request }, info) => {
  const names: string[] = []
  page.on('request', request => {
    if (request.url().endsWith('/mcp') && request.method() === 'POST') {
      const body = request.postDataJSON()
      if (body.method === 'tools/call') names.push(body.params.name)
    }
    expect(request.url()).not.toContain('/ai/')
  })
  await page.goto('/monitor')
  const message = '直接输入 "带引号"'
  await page.getByLabel('命令输入', { exact: true }).fill(`set example.params.message ${JSON.stringify(message)}\npolicy example example`)
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect(page.getByRole('status')).toContainText('命令批次已执行')
  expect(names.filter(name => name === 'tui_command')).toHaveLength(1)
  expect(names).not.toContain('take')
  expect(names).not.toContain('policies')
  const after = await (await request.get(`${rc}/health`)).json()
  expect(after.control_owner).toBe('policy')
  expect((await (await request.get(`${api}/test/requests`)).json())).toEqual([])
  await page.getByRole('button', { name: '展开操作回执' }).click()
  await expect(page.getByLabel('操作回执', { exact: true })).toContainText('tui_batch')
  expect((await (await request.get(`${api}/test/batch-state`)).json()).config.params.message).toBe(message)
  for (const camera of ['head', 'left_wrist', 'right_wrist']) {
    await expect(page.locator(`[data-camera="${camera}"] .camera-state`)).toHaveAttribute('data-state', 'live')
  }
  await page.screenshot({ path: info.outputPath('direct-command-mode.png') })
})

test('direct text rejects recursion before earlier lines can execute', async ({ page, request }) => {
  await page.goto('/monitor')
  await page.getByLabel('命令输入', { exact: true }).fill('record on\nmcp take')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect(page.getByRole('status')).toHaveAttribute('data-error', 'true')
  const after = await (await request.get(`${rc}/health`)).json()
  expect(after.recording.state).toBe('idle')
  expect(after.control_owner).toBe('policy')
})

test('direct text stop leaves started line to finish and cancels its tail', async ({ page, request }) => {
  await request.post(`${api}/test/batch-hold`)
  await page.goto('/monitor')
  await page.getByLabel('命令输入', { exact: true }).fill('set example.params.message "first"\nset example.params.message "tail"')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect.poll(async () => (await (await request.get(`${api}/test/batch-state`)).json()).held).toBe(true)
  const stopped = page.waitForRequest(request => request.method() === 'POST' && request.url().endsWith('/mcp')
    && request.postDataJSON()?.params?.name === 'cancel_tui_command')
  await page.getByRole('button', { name: '停止后续步骤' }).click()
  await stopped
  await request.post(`${api}/test/batch-release`)
  await expect(page.getByRole('status')).toHaveAttribute('data-error', 'true')
  expect((await (await request.get(`${api}/test/batch-state`)).json()).config.params.message).toBe('first')
})

test('direct text refresh recovers the parent receipt without cancellation or resubmission', async ({ page, request }) => {
  await request.post(`${api}/test/batch-hold`)
  const calls: string[] = []
  page.on('request', request => {
    if (request.url().endsWith('/mcp') && request.method() === 'POST') {
      const body = request.postDataJSON()
      if (body.method === 'tools/call') calls.push(body.params.name)
    }
  })
  await page.goto('/monitor')
  await page.getByLabel('命令输入', { exact: true }).fill('set example.params.message "survived refresh"\nstatus')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect.poll(() => page.evaluate(() => JSON.parse(sessionStorage.getItem('rc-pending-operation') || '{}').id)).toBeTruthy()
  await expect.poll(async () => (await (await request.get(`${api}/test/batch-state`)).json()).held).toBe(true)
  await page.reload()
  await expect(page.getByRole('button', { name: '查询原回执' })).toBeVisible()
  await request.post(`${api}/test/batch-release`)
  await expect.poll(async () => {
    await page.getByRole('button', { name: '查询原回执' }).click()
    return await page.getByRole('status').textContent()
  }).toContain('操作回执已确认')
  expect(calls.filter(name => name === 'tui_command')).toHaveLength(1)
  expect(calls).not.toContain('cancel_tui_command')
})

test('direct text stop during initial status read prevents submission', async ({ page }) => {
  let release!: () => void, entered!: () => void
  const held = new Promise<void>(resolve => { release = resolve })
  const statusRead = new Promise<void>(resolve => { entered = resolve })
  let batches = 0
  await page.route('**/mcp', async route => {
    const request = route.request()
    const body = request.method() === 'POST' ? request.postDataJSON() : {}
    if (body?.method === 'tools/call' && body.params.name === 'status') {
      entered()
      await held
    }
    if (body?.method === 'tools/call' && body.params.name === 'tui_command') batches++
    await route.continue()
  })
  await page.goto('/monitor')
  await page.getByLabel('命令输入', { exact: true }).fill('reset')
  await page.getByRole('button', { name: '发送指令' }).click()
  await statusRead
  await page.getByRole('button', { name: '停止后续步骤' }).click()
  release()
  await expect(page.getByRole('status')).toContainText('已取消提交')
  expect(batches).toBe(0)
})
