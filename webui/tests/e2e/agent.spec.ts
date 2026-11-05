import { expect, test } from '@playwright/test'
const api = process.env.RC_WEBUI_TEST_API || 'http://127.0.0.1:18766'
const rc = process.env.RC_WEBUI_E2E_URL || 'http://127.0.0.1:18765'
test.beforeEach(async ({ request }) => { expect((await request.post(`${api}/test/reset`)).ok()).toBeTruthy() })

test('discovers typed policy commands every round, applies effort and carries only ended conversation', async ({ page, request }) => {
  await page.route('**/config.json', route => route.fulfill({ json: {
    enabled: true, model: 'test-translator', timeout_s: 3, effort: 'high',
  } }))
  const calls: any[] = []
  page.on('request', req => {
    if (req.url().endsWith('/mcp') && req.method() === 'POST') {
      const body = req.postDataJSON()
      if (body.method === 'tools/call') calls.push(body.params)
    }
  })
  await page.goto('/monitor')
  await page.getByLabel('自然语言指令').fill('设置消息')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect(page.getByRole('status')).toContainText('消息已更新并查询核实')
  let requests = await (await request.get(`${api}/test/requests`)).json()
  expect(requests.map((r: any) => r.decision.tool_call?.name ?? null)).toEqual(['show', 'take', 'policy_command', 'show', null])
  for (const r of requests) {
    expect(r.effort).toBe('high')
    expect(r.stream).toBe(true)
    const capabilities = r.context.capabilities
    expect(capabilities.instructions).toBeTruthy()
    expect(capabilities.tools.length).toBeGreaterThan(15)
    expect(capabilities.tools.every((tool: any) => tool.description && tool.inputSchema && tool.annotations)).toBe(true)
    expect(capabilities.tools.map((tool: any) => tool.name)).not.toContain('tui_command')
    expect(JSON.stringify(capabilities.policy_commands)).toContain('热更新本实例的消息')
    expect(capabilities.props.units.epos).toContain('meters')
  }
  const write = calls.find(call => call.name === 'policy_command')
  expect(write.arguments).toMatchObject({ instance_id: 'example', command: 'set-message', args: ['浏览器验证消息'], wait: false,
    expected_control: { run_id: expect.any(String), generation: expect.any(Number), control_epoch: expect.any(Number) } })
  expect((await (await request.get(`${api}/test/batch-state`)).json()).config.params.message).toBe('浏览器验证消息')
  expect((await (await request.get(`${rc}/health`)).json()).state).toBe('paused')
  await page.getByLabel('自然语言指令').fill('刚才那个策略再设置消息')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect(page.locator('.ai-entry')).toHaveCount(2)
  await expect(page.getByRole('status')).toContainText('消息已更新并查询核实')
  requests = await (await request.get(`${api}/test/requests`)).json()
  expect(requests[5].context.recent_conversation[0].input).toBe('设置消息')
  expect(JSON.stringify(requests[5].context.recent_conversation)).not.toContain('reasoning')
})

test('chain apply refreshes separate instance descriptions before calling the new instance', async ({ page, request }) => {
  await page.goto('/monitor')
  await page.getByLabel('自然语言指令').fill('链后设置消息')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect(page.getByRole('status')).toContainText('example-1 消息已更新', { timeout: 20_000 })
  const requests = await (await request.get(`${api}/test/requests`)).json()
  const next = requests.find((r: any) => r.decision.tool_call?.name === 'policy_command')
  expect(next.context.capabilities.policy_commands.policies.filter((p: any) => p.loaded).map((p: any) => p.instance_id)).toEqual(['example-0', 'example-1'])
  expect(next.decision.tool_call.arguments.instance_id).toBe('example-1')
  expect((await (await request.get(`${rc}/health`)).json()).control_owner).toBe('mcp')
  await page.getByLabel('自然语言指令').fill('重复实例改 example')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect(page.getByRole('status')).toContainText('请明确选择 example-0 还是 example-1')
  await expect(page.locator('.composer-shell')).toHaveAttribute('data-phase', 'idle')
})

for (const outcome of ['completed', 'terminated']) test(`policy episode reports ${outcome} through the correct receipt`, async ({ page, request }) => {
  const calls: string[] = []
  page.on('request', req => {
    if (req.url().endsWith('/mcp') && req.method() === 'POST') {
      const body = req.postDataJSON()
      if (body.method === 'tools/call') calls.push(body.params.name)
    }
  })
  await page.goto('/monitor')
  await page.getByLabel('自然语言指令').fill(outcome === 'completed' ? '运行并等待任务完成' : '运行并等待任务终止')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect.poll(async () => (await (await request.get(`${rc}/health`)).json()).state).toBe('running')
  await expect.poll(() => page.evaluate(() => JSON.parse(sessionStorage.getItem('rc-pending-operation') || '{}').kind)).toBe('policy_episode')
  expect((await request.post(`${api}/test/policy-result`, { data: { outcome } })).ok()).toBe(true)
  await expect(page.getByRole('status')).toContainText(outcome === 'completed' ? '策略任务已完成' : '策略任务已终止')
  expect(calls).toContain('get_policy_result')
  expect(calls.filter(name => name === 'return_and_wait')).toHaveLength(1)
  await expect(page.locator('.composer-shell')).toHaveAttribute('data-phase', outcome === 'completed' ? 'success' : 'idle')
})

test('refresh restores a waiting policy episode without resubmitting or resuming the old loop', async ({ page, request }) => {
  await page.goto('/monitor')
  await page.getByLabel('自然语言指令').fill('运行并等待任务终止')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect.poll(async () => (await (await request.get(`${rc}/health`)).json()).state).toBe('running')
  await expect.poll(() => page.evaluate(() => JSON.parse(sessionStorage.getItem('rc-pending-operation') || '{}').id)).toBeTruthy()
  await page.reload()
  await expect(page.getByRole('button', { name: '查询原回执' })).toBeVisible()
  expect((await request.post(`${api}/test/policy-result`, { data: { outcome: 'terminated' } })).ok()).toBe(true)
  await expect.poll(async () => (await (await request.get(`${rc}/health`)).json()).state).toBe('paused')
  await page.getByRole('button', { name: '查询原回执' }).click()
  await expect(page.getByRole('status')).toContainText('策略任务已终止，未报告完成；旧清单不会继续')
  await expect(page.locator('.composer-shell')).toHaveAttribute('data-phase', 'idle')
  expect((await (await request.get(`${api}/test/requests`)).json())).toHaveLength(2)
})

test('relative motion and state queries force text-only responses with guarded execution', async ({ page }) => {
  const calls: any[] = []
  page.on('request', req => {
    if (req.url().endsWith('/mcp') && req.method() === 'POST') {
      const body = req.postDataJSON()
      if (body.method === 'tools/call') calls.push(body.params)
    }
  })
  await page.goto('/monitor')
  await page.getByLabel('自然语言指令').fill('左臂上移一毫米并读取反馈')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect(page.getByRole('status')).toContainText('已发送左臂上移一毫米的目标并读取反馈')
  expect(calls.find(call => call.name === 'send_command').arguments).toMatchObject({
    commands: 'left:up:0.001@slow', include_images: false, wait: false, expected_control: { run_id: expect.any(String) },
  })
  expect(calls.find(call => call.name === 'state').arguments.include_images).toBe(false)
})

test('malformed submission response remains unknown and never retries a write', async ({ page, request }) => {
  let writes = 0
  await page.route('**/mcp', async route => {
    const req = route.request(), body = req.method() === 'POST' ? req.postDataJSON() : {}
    if (body?.method === 'tools/call' && body.params.name === 'take') {
      writes++
      await route.fulfill({ json: { jsonrpc: '2.0', id: body.id, result: { content: [{ type: 'text', text: 'malformed receipt' }] } } })
    } else await route.continue()
  })
  await page.goto('/monitor')
  await page.getByLabel('自然语言指令').fill('复位')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect(page.locator('.composer-shell')).toHaveAttribute('data-phase', 'pending')
  await expect(page.getByRole('status')).toContainText('提交是否受理需核对')
  expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem('rc-pending-operation') || '{}').uncertain)).toBe(true)
  expect(writes).toBe(1)
  expect((await (await request.get(`${api}/test/requests`)).json())).toHaveLength(1)
})
