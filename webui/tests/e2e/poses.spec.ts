import { expect, test } from '@playwright/test'
import { writeFile } from 'node:fs/promises'

test('pose pages stream feedback and retain all charts while navigating and resizing', async ({ page }, info) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/joints')
  await expect(page).toHaveTitle('Robot Control · 关节位姿')
  await expect(page.locator('.pose-chart')).toHaveCount(14)
  await expect(page.locator('.pose-page')).toHaveAttribute('data-connected', 'true')
  await expect.poll(async () => Number(await page.locator('.pose-page').getAttribute('data-samples'))).toBeGreaterThan(30)
  for (const width of [1440, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 900 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    const geometry = await page.locator('.pose-arm').evaluateAll(arms => arms.map(arm => {
      const rect = arm.getBoundingClientRect()
      return { top: rect.top, bottom: rect.bottom, charts: [...arm.querySelectorAll('canvas')].map(canvas => {
        const box = canvas.getBoundingClientRect(); return { width: box.width, height: box.height }
      }) }
    }))
    expect(geometry[1].top).toBeGreaterThanOrEqual(geometry[0].bottom)
    for (const arm of geometry) for (const chart of arm.charts) expect(Math.abs(chart.width - chart.height)).toBeLessThan(2)
    await expect(page.getByLabel('自然语言指令')).toBeInViewport()
    await expect(page.getByRole('tab', { name: '末端位姿' })).toBeInViewport()
    await page.screenshot({ path: info.outputPath(`joints-${width}.png`) })
  }
  await page.setViewportSize({ width: 1440, height: 900 })
  const before = Number(await page.locator('.pose-page').getAttribute('data-samples'))
  await page.getByRole('tab', { name: '末端位姿' }).click()
  await expect(page).toHaveURL(/\/eef$/)
  await expect(page).toHaveTitle('Robot Control · 末端位姿')
  await expect(page.locator('.pose-chart')).toHaveCount(14)
  await expect(page.getByTestId('eef-left-0')).toContainText('X')
  await expect.poll(async () => Number(await page.locator('.pose-page').getAttribute('data-samples'))).toBeGreaterThan(before)
  await page.screenshot({ path: info.outputPath('eef-desktop.png') })
  await page.getByRole('tab', { name: '监控' }).click()
  await expect(page.getByTestId('camera-head')).toHaveAttribute('data-ready', 'true')
  await page.getByRole('tab', { name: '关节位姿' }).click()
  await expect.poll(async () => Number(await page.locator('.pose-page').getAttribute('data-samples'))).toBeGreaterThan(before)
  expect(errors).toEqual([])
})

test('wheel freezes only one chart and latest restores live following', async ({ page }) => {
  // A real Worker consumes a finite SSE fixture spanning more than a minute.
  const packets = Array.from({ length: 4001 }, (_, i) => `data: ${JSON.stringify({ run_id: 'history', sequence: i + 1,
    sampled_monotonic: i * .02, error: null, state: {
      qpos: Array.from({ length: 14 }, (_, ch) => Math.sin(i / 50 + ch)),
      eef: Array.from({ length: 14 }, (_, ch) => .2 * Math.cos(i / 50 + ch)),
      sequences: { left: i + 1, right: i + 1 }, timestamps: { left: i * .02, right: i * .02 }, feedback_age_s: { left: 0, right: 0 },
    } })}\n\n`).join('')
  let advance = false, sequence = 4001
  await page.route('**/state/stream', route => {
    const next = advance ? `data: ${JSON.stringify({ run_id: 'history', sequence: ++sequence,
      sampled_monotonic: (sequence - 1) * .02, error: null, state: {
        qpos: Array(14).fill(.5), eef: Array(14).fill(.2), sequences: { left: sequence, right: sequence },
        timestamps: { left: sequence * .02, right: sequence * .02 }, feedback_age_s: { left: 0, right: 0 },
      } })}\n\n` : packets
    return route.fulfill({ contentType: 'text/event-stream', body: 'retry: 100\n\n' + next })
  })
  await page.goto('/joints')
  await expect.poll(async () => Number(await page.locator('.pose-page').getAttribute('data-samples'))).toBe(4001)
  const chart = page.getByTestId('joints-left-0')
  await chart.locator('canvas').hover()
  await page.mouse.wheel(0, -100)
  await expect(chart).toHaveAttribute('data-following', 'false')
  const start = await chart.getAttribute('data-start')
  await expect(page.getByTestId('joints-left-1')).toHaveAttribute('data-following', 'true')
  advance = true
  await expect.poll(async () => Number(await page.locator('.pose-page').getAttribute('data-samples'))).toBeGreaterThan(4001)
  await expect(chart).toHaveAttribute('data-start', start!)
  await page.getByRole('tab', { name: '末端位姿' }).click()
  await page.getByRole('tab', { name: '关节位姿' }).click()
  await expect(chart).toHaveAttribute('data-start', start!)
  await expect(chart).toHaveAttribute('data-following', 'false')
  await chart.getByRole('button', { name: '回到实时' }).click()
  await expect(chart).toHaveAttribute('data-following', 'true')
})

test('real HTTP state delivery sustains at least 30Hz while charts and cameras are active', async ({ page, request }, info) => {
  test.setTimeout(45000)
  await page.goto('/joints')
  const panel = page.locator('.pose-page')
  await expect(panel).toHaveAttribute('data-connected', 'true')
  await expect.poll(async () => Number(await panel.getAttribute('data-samples'))).toBeGreaterThan(50)
  const start = await panel.evaluate(el => ({ count: Number(el.getAttribute('data-samples')), at: performance.now() }))
  await page.waitForTimeout(20000)
  const end = await panel.evaluate(el => ({ count: Number(el.getAttribute('data-samples')), at: performance.now() }))
  const evidence = { samples: end.count - start.count, seconds: (end.at - start.at) / 1000,
    hz: (end.count - start.count) * 1000 / (end.at - start.at) }
  await info.attach('state-frequency.json', { body: JSON.stringify(evidence, null, 2), contentType: 'application/json' })
  await writeFile(info.outputPath('state-frequency.json'), JSON.stringify(evidence, null, 2))
  expect(evidence.hz).toBeGreaterThanOrEqual(30)
  const value = await page.getByTestId('joints-left-6').locator('.pose-value').innerText()
  const api = process.env.RC_WEBUI_TEST_API || 'http://127.0.0.1:18766'
  expect((await request.post(`${api}/test/disconnect-state`)).ok()).toBe(true)
  await expect(page.locator('.pose-error')).toContainText('状态流')
  await expect(page.getByTestId('joints-left-6').locator('.pose-value')).toHaveText(value)
  await expect(panel).toHaveAttribute('data-connected', 'true', { timeout: 10000 })
  await expect.poll(async () => Number(await panel.getAttribute('data-samples'))).toBeGreaterThan(end.count)
})

test('three tabs share transport, retain separate histories and survive the receiving tab closing', async ({ page, context, request }) => {
  await page.goto('/joints')
  await expect.poll(async () => Number(await page.locator('.pose-page').getAttribute('data-samples'))).toBeGreaterThan(60)
  const second = await context.newPage()
  await second.goto('/eef')
  await expect(second.locator('.pose-page')).toHaveAttribute('data-connected', 'true')
  const third = await context.newPage()
  await third.goto('/monitor')
  for (const camera of ['head', 'left_wrist', 'right_wrist']) {
    await expect(third.getByTestId(`camera-${camera}`)).toHaveAttribute('data-ready', 'true')
  }
  const rc = process.env.RC_WEBUI_E2E_URL || 'http://127.0.0.1:18765'
  await expect.poll(async () => {
    const data = await (await request.get(`${rc}/preview/status`)).json()
    return Object.values(data.cameras).map((value: any) => value.subscribers)
  }).toEqual([1, 1, 1])
  const firstCount = Number(await page.locator('.pose-page').getAttribute('data-samples'))
  const secondCount = Number(await second.locator('.pose-page').getAttribute('data-samples'))
  expect(firstCount).toBeGreaterThan(secondCount)
  await page.close()
  await expect.poll(async () => Number(await second.locator('.pose-page').getAttribute('data-samples'))).toBeGreaterThan(secondCount + 30)
  await expect(third.getByRole('button', { name: '导出头部视频' })).toBeEnabled()
  await second.reload()
  await expect(second.locator('.pose-page')).toHaveAttribute('data-connected', 'true')
  await expect(third.getByTestId('camera-head')).toHaveAttribute('data-ready', 'true')
  await second.close(); await third.close()
})
