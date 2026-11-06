import { expect, test } from '@playwright/test'
import { readFile } from 'node:fs/promises'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

const api = process.env.RC_WEBUI_TEST_API || 'http://127.0.0.1:18766'
const rc = process.env.RC_WEBUI_E2E_URL || 'http://127.0.0.1:18765'
test.beforeEach(async ({ request }) => { expect((await request.post(`${api}/test/reset`)).ok()).toBeTruthy() })

test('single-camera routes preserve all subscriptions and export playable 30fps video repeatedly', async ({ page, request }, info) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  await page.goto('/monitor')
  await expect(page.getByRole('button', { name: '导出头部视频' })).toBeEnabled()
  const before = await (await request.get(`${rc}/health`)).json()
  await page.getByRole('link', { name: '放大头部画面' }).click()
  await expect(page).toHaveURL(/\/monitor\/head$/)
  await expect(page.locator('.camera-tile')).toHaveCount(1)
  await expect(page.getByRole('tab', { name: '监控' })).toHaveAttribute('data-state', 'active')
  await expect(page.getByLabel('自然语言指令')).toBeVisible()
  await expect(page.getByRole('button', { name: '导出头部视频' })).toBeInViewport()
  for (const camera of ['left_wrist', 'head', 'right_wrist']) {
    const data = await (await request.get(`${rc}/preview/status`)).json()
    expect(data.cameras[camera].subscribers).toBe(1)
  }
  let previousDuration = 0
  for (let i = 0; i < 2; i++) {
    await page.waitForTimeout(1100)
    const download = page.waitForEvent('download')
    await page.getByRole('button', { name: '导出头部视频' }).click()
    const file = await download, path = info.outputPath(`head-${i}.webm`)
    expect(file.suggestedFilename()).toMatch(/head.*\.webm$/)
    await file.saveAs(path)
    const probe = JSON.parse((await promisify(execFile)('ffprobe', ['-v', 'error', '-count_frames', '-show_streams', '-show_format', '-of', 'json', path])).stdout)
    const video = probe.streams[0]
    expect(video.codec_name).toBe('vp8'); expect(video.width).toBe(640); expect(video.height).toBe(480)
    expect(video.r_frame_rate).toBe('30/1')
    const duration = Number(probe.format.duration), frames = Number(video.nb_read_frames)
    expect(Math.abs(duration - frames / 30)).toBeLessThan(.002)
    expect(duration).toBeGreaterThan(previousDuration)
    previousDuration = duration
    const playback = await page.evaluate(async bytes => {
      const url = URL.createObjectURL(new Blob([new Uint8Array(bytes)], { type: 'video/webm' })), video = document.createElement('video')
      video.src = url; video.muted = true
      try {
        await new Promise<void>((resolve, reject) => { video.onloadedmetadata = () => resolve(); video.onerror = reject })
        await new Promise<void>((resolve, reject) => { video.onseeked = () => resolve(); video.onerror = reject; video.currentTime = video.duration / 2 })
        return { duration: video.duration, at: video.currentTime, width: video.videoWidth }
      } finally { video.removeAttribute('src'); video.load(); URL.revokeObjectURL(url) }
    }, [...await readFile(path)])
    expect(playback.width).toBe(640); expect(playback.at).toBeGreaterThan(0)
  }
  const after = await (await request.get(`${rc}/health`)).json()
  expect(after.generation).toBe(before.generation); expect(after.control_owner).toBe(before.control_owner)
  expect(after.recording.state).toBe(before.recording.state)
  await page.getByRole('tab', { name: '监控' }).click()
  await expect(page).toHaveURL(/\/monitor$/)
  await expect(page.locator('.camera-tile')).toHaveCount(3)
  await page.goto('/monitor/missing')
  await expect(page).toHaveURL(/\/monitor$/)
  await page.goto('/monitor/right_wrist')
  await expect(page.locator('.camera-tile')).toHaveAttribute('data-camera', 'right_wrist')
  await page.reload()
  await expect(page.getByTestId('camera-right_wrist')).toHaveAttribute('data-ready', 'true')
  expect(errors).toEqual([])
  await page.screenshot({ path: info.outputPath('single-camera.png') })
})

test('exports full AI attempts and receipts while a folded transcript keeps recording', async ({ page }, info) => {
  await page.goto('/monitor')
  const exportLog = page.getByRole('button', { name: '导出 AI 运行日志' })
  await expect(exportLog).toBeDisabled()
  await page.getByLabel('自然语言指令').fill('查询状态')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect(page.getByRole('status')).toContainText('已执行')
  await page.getByRole('button', { name: '收起 AI 日志' }).click()
  await expect(page.getByLabel('AI 输出历史')).toBeHidden()
  await page.getByLabel('自然语言指令').fill('非法指令')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect(page.getByRole('status')).toHaveAttribute('data-error', 'true')
  await expect(page.getByLabel('AI 输出历史')).toBeHidden()
  const waiting = page.waitForEvent('download')
  await exportLog.click()
  const download = await waiting, path = info.outputPath('ai.json')
  await download.saveAs(path)
  const log = JSON.parse(await readFile(path, 'utf8'))
  expect(log.complete).toBe(true)
  expect(log.events.filter((e: any) => e.type === 'request.started')).toHaveLength(2)
  expect(log.events.some((e: any) => e.type === 'model.request')).toBe(true)
  expect(log.events.some((e: any) => e.type === 'mcp.response')).toBe(true)
  expect(log.events.some((e: any) => e.type === 'model.error')).toBe(true)
  expect(JSON.stringify(log)).not.toContain('local-test-only')
  await page.getByRole('button', { name: '展开 AI 日志' }).click()
  await expect(page.getByLabel('AI 输出历史')).toContainText('非法指令')
  for (const width of [320, 390, 768]) {
    await page.setViewportSize({ width, height: 844 })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await expect(exportLog).toBeInViewport()
    await expect(page.getByLabel('自然语言指令')).toBeInViewport()
    await page.screenshot({ path: info.outputPath(`export-ui-${width}.png`) })
  }
})

test('fills a missing stream with the previous image and reconnects without changing the video timebase', async ({ page }, info) => {
  const colors = await page.evaluate(() => ['red', 'blue'].map(color => {
    const canvas = document.createElement('canvas'); canvas.width = 640; canvas.height = 480
    const ctx = canvas.getContext('2d')!; ctx.fillStyle = color; ctx.fillRect(0, 0, 640, 480)
    return canvas.toDataURL('image/jpeg').split(',')[1]!
  }))
  let connections = 0
  await page.route('**/preview/head.mjpg', route => {
    const jpeg = Buffer.from(colors[connections++ === 0 ? 0 : 1]!, 'base64')
    return route.fulfill({ status: 200, headers: { 'Content-Type': 'multipart/x-mixed-replace; boundary=frame' }, body: Buffer.concat([
      Buffer.from(`--frame\r\nContent-Length: ${jpeg.length}\r\nContent-Type: image/jpeg\r\n\r\n`), jpeg, Buffer.from('\r\n'),
    ]) })
  })
  await page.goto('/monitor/head')
  await expect(page.getByTestId('camera-head')).toHaveAttribute('data-ready', 'true')
  await expect.poll(() => connections).toBeGreaterThanOrEqual(2)
  await page.waitForTimeout(700)
  const waiting = page.waitForEvent('download')
  await page.getByRole('button', { name: '导出头部视频' }).click()
  const file = await waiting, path = info.outputPath('gap.webm'); await file.saveAs(path)
  const pixel = async (at: string) => {
    const { stdout } = await promisify(execFile)('ffmpeg', ['-v', 'error', '-ss', at, '-i', path, '-frames:v', '1', '-vf', 'scale=1:1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'], { encoding: 'buffer' })
    return [...stdout]
  }
  const old = await pixel('1.5'), resumed = await pixel('2.5')
  expect(old[0]).toBeGreaterThan(220); expect(old[2]).toBeLessThan(30)
  expect(resumed[2]).toBeGreaterThan(220); expect(resumed[0]).toBeLessThan(30)
})

test('downloads an in-progress log then retains cancellation and partial model text', async ({ page, request }, info) => {
  await request.post(`${api}/test/stream-hold`)
  await page.goto('/monitor')
  await page.getByLabel('自然语言指令').fill('流式等待 复位')
  await page.getByRole('button', { name: '发送指令' }).click()
  await expect(page.locator('.composer-shell')).toHaveAttribute('data-phase', 'streaming')
  async function exported(name: string) {
    const waiting = page.waitForEvent('download')
    await page.getByRole('button', { name: '导出 AI 运行日志' }).click()
    const download = await waiting, path = info.outputPath(name); await download.saveAs(path)
    return JSON.parse(await readFile(path, 'utf8'))
  }
  const before = await exported('running.json')
  expect(before.events.some((event: any) => event.type === 'model.response.chunk')).toBe(true)
  expect(before.events.some((event: any) => event.type === 'request.finished')).toBe(false)
  await page.getByRole('button', { name: '停止后续步骤' }).click()
  await expect(page.locator('.composer-shell')).toHaveAttribute('data-phase', 'cancelled')
  const cancelled = await exported('cancelled.json')
  expect(cancelled.events.some((event: any) => event.type === 'request.cancelled')).toBe(true)
  expect(cancelled.events.find((event: any) => event.type === 'request.finished').data.outcome).toBe('cancelled')
  await request.post(`${api}/test/stream-release`)
})

test('camera caches are isolated across tabs and a reload cleans only abandoned sessions', async ({ page, context }) => {
  await page.goto('/monitor')
  await expect(page.getByRole('button', { name: '导出头部视频' })).toBeEnabled()
  const original = await page.evaluate(async () => (await indexedDB.databases()).map(value => value.name).filter(name => name?.startsWith('rc-webui-video-')))
  expect(original).toHaveLength(3)
  const second = await context.newPage()
  await second.goto('/monitor/head')
  await expect(second.getByRole('button', { name: '导出头部视频' })).toBeEnabled()
  const both = await second.evaluate(async () => (await indexedDB.databases()).map(value => value.name).filter(name => name?.startsWith('rc-webui-video-')))
  expect(both).toHaveLength(6)
  expect(original.every(name => both.includes(name))).toBe(true)
  await second.reload()
  await expect(second.getByRole('button', { name: '导出头部视频' })).toBeEnabled()
  await expect.poll(async () => (await second.evaluate(() => indexedDB.databases())).length).toBe(6)
  const reloaded = await second.evaluate(async () => (await indexedDB.databases()).map(value => value.name))
  expect(original.every(name => reloaded.includes(name))).toBe(true)
  await second.close()
})

for (const failure of ['unsupported', 'storage']) test(`keeps monitoring and reports ${failure} recording failure`, async ({ page }, info) => {
  // Inject the platform fault in the real worker realm; normal streaming and muxing stay real.
  await page.route(/\/(?:src\/lib\/camera\/worker\.ts|assets\/worker-[^/]+\.js)(?:\?.*)?$/, async route => {
    const response = await route.fetch()
    const fault = failure === 'unsupported' ? 'self.VideoEncoder = undefined;'
      : `const originalAdd = IDBObjectStore.prototype.add; let packetWrites = 0;
         IDBObjectStore.prototype.add = function(...args) {
           if (this.name === 'packets' && ++packetWrites > 21) throw new DOMException('测试存储容量耗尽', 'QuotaExceededError');
           return originalAdd.apply(this, args);
         };`
    await route.fulfill({ response, body: fault + '\n' + await response.text() })
  })
  await page.goto('/monitor/head')
  await expect(page.getByTestId('camera-head')).toHaveAttribute('data-ready', 'true')
  await expect(page.locator('.camera-error')).toContainText(failure === 'unsupported' ? '不支持录像' : '存储容量耗尽')
  const first = await page.getByTestId('camera-head').getAttribute('data-sequence')
  await expect.poll(() => page.getByTestId('camera-head').getAttribute('data-sequence')).not.toBe(first)
  const button = page.getByRole('button', { name: '导出头部视频' })
  if (failure === 'unsupported') await expect(button).toBeDisabled()
  else {
    await expect(button).toBeEnabled()
    const waiting = page.waitForEvent('download'); await button.click()
    const file = await waiting, path = info.outputPath('partial.webm'); await file.saveAs(path)
    expect(file.suggestedFilename()).toContain('-partial.webm')
    const probe = JSON.parse((await promisify(execFile)('ffprobe', ['-v', 'error', '-count_frames', '-show_streams', '-of', 'json', path])).stdout)
    expect(Number(probe.streams[0].nb_read_frames)).toBe(18)
  }
})

test('keeps the video clock through a suspended page and catches up on resume', async ({ page }) => {
  await page.goto('/monitor/head')
  await expect(page.getByRole('button', { name: '导出头部视频' })).toBeEnabled()
  const head = page.locator('[data-camera="head"]')
  const before = Number(await head.getAttribute('data-recorded-frames'))
  const devtools = await page.context().newCDPSession(page)
  await devtools.send('Page.setWebLifecycleState', { state: 'frozen' })
  await new Promise(resolve => setTimeout(resolve, 2200))
  await devtools.send('Page.setWebLifecycleState', { state: 'active' })
  await page.bringToFront()
  await expect.poll(async () => Number(await head.getAttribute('data-recorded-frames'))).toBeGreaterThan(before + 60)
  await expect(head.locator('.camera-error')).toHaveCount(0)
  await expect.poll(async () => Number(await head.getAttribute('data-pending-frames'))).toBeLessThan(30)
})
