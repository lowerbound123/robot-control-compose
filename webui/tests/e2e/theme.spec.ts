import { expect, test } from '@playwright/test'

test('manual theme switches all pages and persists without restarting live stores', async ({ page }, info) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  // Keep the existing light theme by default even on a dark operating system.
  await page.emulateMedia({ colorScheme: 'dark' })
  await page.goto('/monitor')
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  await expect(page.getByTestId('camera-head')).toHaveAttribute('data-ready', 'true')
  const frames = Number(await page.locator('[data-camera=head]').getAttribute('data-recorded-frames'))
  const databases = await page.evaluate(async () => (await indexedDB.databases()).map(db => db.name).sort())
  await page.getByRole('button', { name: '切换到深色主题' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await expect(page.locator('html')).toHaveCSS('color-scheme', 'dark')
  await expect(page.getByRole('button', { name: '切换到浅色主题' })).toBeVisible()
  expect(await page.evaluate(() => localStorage.getItem('rc-webui-theme'))).toBe('dark')
  expect(await page.evaluate(async () => (await indexedDB.databases()).map(db => db.name).sort())).toEqual(databases)
  await expect.poll(async () => Number(await page.locator('[data-camera=head]').getAttribute('data-recorded-frames'))).toBeGreaterThan(frames)
  await page.screenshot({ path: info.outputPath('dark-monitor.png') })

  for (const [tab, url] of [['关节位姿', '/joints'], ['末端位姿', '/eef']] as const) {
    await page.getByRole('tab', { name: tab }).click()
    await expect(page).toHaveURL(new RegExp(`${url}$`))
    await expect(page.locator('.pose-chart')).toHaveCount(14)
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    await expect.poll(async () => Number(await page.locator('.pose-page').getAttribute('data-samples'))).toBeGreaterThan(30)
    const plot = page.locator('.pose-plot canvas').first()
    // Inspect actual canvas strokes, not just the surrounding CSS.
    await expect.poll(() => plot.evaluate((canvas: HTMLCanvasElement) => {
      const data = canvas.getContext('2d')!.getImageData(0, 0, canvas.width, canvas.height).data
      let pixels = 0
      for (let i = 0; i < data.length; i += 4) if (data[i] > 100 && data[i + 1] > 150 && data[i + 2] > 210
        && data[i + 2] - data[i] > 40 && data[i + 3] > 100) pixels++
      return pixels
    })).toBeGreaterThan(0)
    await page.screenshot({ path: info.outputPath(`dark-${url.slice(1)}.png`) })
  }
  const samples = Number(await page.locator('.pose-page').getAttribute('data-samples'))
  await page.getByRole('button', { name: '切换到浅色主题' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  await expect.poll(async () => Number(await page.locator('.pose-page').getAttribute('data-samples'))).toBeGreaterThan(samples)
  await page.getByRole('button', { name: '切换到深色主题' }).focus()
  await page.keyboard.press('Enter')
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await page.reload()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  for (const width of [1280, 1201, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 900 })
    await expect(page.getByRole('button', { name: '切换到浅色主题' })).toBeInViewport()
    await expect(page.getByLabel('自然语言指令')).toBeInViewport()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  }
  await page.screenshot({ path: info.outputPath('dark-mobile.png') })
  expect(errors).toEqual([])
})

test('saved theme applies before app startup and synchronizes across tabs', async ({ page, context }) => {
  await page.addInitScript(() => localStorage.setItem('rc-webui-theme', 'dark'))
  await page.route('**/src/main.ts', route => route.abort())
  await page.goto('/')
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await expect(page.locator('html')).toHaveCSS('color-scheme', 'dark')
  await page.unroute('**/src/main.ts')
  await page.reload()
  const second = await context.newPage()
  await second.goto('/joints')
  await expect(second.locator('html')).toHaveAttribute('data-theme', 'dark')
  await page.getByRole('button', { name: '切换到浅色主题' }).click()
  await expect(second.locator('html')).toHaveAttribute('data-theme', 'light')
  await second.close()
})

test('theme remains usable when browser preference storage is unavailable', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.addInitScript(() => {
    // Vite's own development client needs storage; fail the app preference path.
    const get = Storage.prototype.getItem, set = Storage.prototype.setItem
    Storage.prototype.getItem = function(key: string) {
      if (key === 'rc-webui-theme') throw new DOMException('Storage blocked', 'SecurityError')
      return get.call(this, key)
    }
    Storage.prototype.setItem = function(key: string, value: string) {
      if (key === 'rc-webui-theme') throw new DOMException('Storage blocked', 'SecurityError')
      return set.call(this, key, value)
    }
  })
  await page.goto('/monitor')
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  await page.getByRole('button', { name: '切换到深色主题' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await page.getByRole('tab', { name: '关节位姿' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await page.getByRole('button', { name: '切换到浅色主题' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  expect(errors).toEqual([])
})

test('theme can change during an AI request while the transcript and result are retained', async ({ page }, info) => {
  await page.goto('/monitor')
  await page.getByLabel('自然语言指令').fill('查询状态')
  await page.getByRole('button', { name: '发送指令' }).click()
  await page.getByRole('button', { name: '切换到深色主题' }).click()
  await expect(page.getByRole('status')).toContainText('已执行')
  await expect(page.getByLabel('AI 输出历史')).toContainText('查询状态')
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
  await page.screenshot({ path: info.outputPath('dark-ai-transcript.png') })
  await page.getByLabel('自然语言指令').fill('非法指令')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect(page.getByRole('status')).toHaveAttribute('data-error', 'true')
  await page.setViewportSize({ width: 390, height: 844 })
  await expect(page.getByRole('button', { name: '切换到浅色主题' })).toBeInViewport()
  await expect(page.getByLabel('自然语言指令')).toBeInViewport()
  await page.screenshot({ path: info.outputPath('dark-ai-error-mobile.png') })
  await page.getByRole('button', { name: '切换到浅色主题' }).click()
  await expect(page.getByLabel('AI 输出历史')).toContainText('查询状态')
  await expect(page.getByLabel('AI 输出历史')).toContainText('非法指令')
})
