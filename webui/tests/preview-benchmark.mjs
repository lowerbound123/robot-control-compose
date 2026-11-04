/** Actual displayed-source-frame measurement against tests/webui_harness.py. */
import { chromium } from '@playwright/test'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

const args = Object.fromEntries(process.argv.slice(2).map((value, i, values) => value.startsWith('--') ? [value.slice(2), values[i + 1]] : []).filter(row => row.length))
if (!args.url || !args.api || !args.output) throw Error('--url, --api and --output are required; target must be the isolated test harness')
const duration = Number(args.seconds || 600)
const output = resolve(args.output)
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ headless: true, args: ['--disable-background-timer-throttling', '--disable-renderer-backgrounding'] })
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
const errors = []
page.on('pageerror', error => errors.push(error.message))
const telemetry = []
const run = promisify(execFile)
let sampling = false
let interval
try {
  await page.goto(args.url + '/monitor', { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(() => document.querySelectorAll('.camera-frame img').length === 3
    && [...document.querySelectorAll('.camera-frame img')].every(image => image.naturalWidth === 640))
  await page.bringToFront()
  await page.screenshot({ path: output + '/desktop.png' })
  const before = await (await fetch(args.url + '/preview/status')).json()
  const clockStart = Date.now()
  const clock = await (await fetch(args.api + '/test/clock')).json()
  const offset = (clockStart + Date.now()) / 2 - clock.now * 1000
  const cameraEpoch = clock.camera_wall_epoch * 1000 + offset
  const session = await page.context().newCDPSession(page)
  await session.send('Performance.enable')
  interval = setInterval(async () => {
    if (sampling) return
    sampling = true
    try {
      const metrics = await session.send('Performance.getMetrics')
      const preview = await (await fetch(args.url + '/preview/status')).json()
      const control = await (await fetch(args.url + '/health')).json()
      let containers = []
      if (args.containers) {
        const result = await run('docker', ['stats', '--no-stream', '--format', '{{json .}}', ...args.containers.split(',')])
        containers = result.stdout.trim().split('\n').filter(Boolean).map(line => JSON.parse(line))
      }
      telemetry.push({ timestamp: Date.now(), metrics: Object.fromEntries(metrics.metrics.map(item => [item.name, item.value])), preview, control, containers })
      await writeFile(output + '/progress.json', JSON.stringify(telemetry.at(-1), null, 2))
    } finally { sampling = false }
  }, 10_000)

  const displayed = await page.evaluate(({ duration, cameraEpoch }) => new Promise(resolve => {
    const cameras = ['left_wrist', 'head', 'right_wrist']
    const tracks = cameras.map(name => {
      const canvas = document.createElement('canvas')
      canvas.width = 144; canvas.height = 12
      return { name, image: document.querySelector(`[data-testid="camera-${name}"]`), canvas,
        context: canvas.getContext('2d', { willReadFrequently: true }), first: null, last: null, count: 0, latencies: [], windows: [], windowCount: 0 }
    })
    let started, windowStart
    function tick(now) {
      started ??= now; windowStart ??= now
      for (const track of tracks) {
        track.context.drawImage(track.image, 0, 0, 144, 12, 0, 0, 144, 12)
        const data = track.context.getImageData(0, 0, 144, 12).data
        let sequence = 0
        for (let bit = 0; bit < 24; bit++) if (data[(6 * 144 + bit * 6 + 3) * 4] > 128) sequence += 2 ** bit
        if (sequence && sequence !== track.last) {
          track.first ??= sequence
          track.last = sequence
          track.count++; track.windowCount++
          track.latencies.push(Date.now() - (cameraEpoch + (sequence - 1) * 1000 / 30))
        }
      }
      if (now - windowStart >= 10_000) {
        for (const track of tracks) {
          track.windows.push({ elapsed_s: (now - started) / 1000, fps: track.windowCount * 1000 / (now - windowStart) })
          track.windowCount = 0
        }
        windowStart = now
      }
      if (now - started < duration * 1000) return requestAnimationFrame(tick)
      resolve({ elapsed_s: (now - started) / 1000, cameras: tracks.map(track => {
        const sorted = [...track.latencies].sort((a, b) => a - b)
        return { name: track.name, frames: track.count, first_sequence: track.first, last_sequence: track.last,
          fps: (track.count - 1) * 1000 / (now - started), latency_p50_ms: sorted[Math.floor(sorted.length * .5)],
          latency_p95_ms: sorted[Math.floor(sorted.length * .95)], latency_max_ms: sorted.at(-1), windows: track.windows }
      }) })
    }
    requestAnimationFrame(tick)
  }), { duration, cameraEpoch })
  clearInterval(interval)
  while (sampling) await new Promise(resolve => setTimeout(resolve, 100))
  const after = await (await fetch(args.url + '/preview/status')).json()
  const report = { duration_s: duration, viewport: { width: 1440, height: 900 }, browser: browser.version(),
    clock_offset_ms: offset, displayed, before, after, telemetry, errors }
  await writeFile(output + '/result.json', JSON.stringify(report, null, 2))
  await page.screenshot({ path: output + '/desktop-final.png' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: output + '/mobile.png', fullPage: true })
  console.log(JSON.stringify({ displayed, errors }, null, 2))
  if (errors.length || displayed.cameras.some(camera => camera.fps < 29.5)) process.exitCode = 1
} finally {
  clearInterval(interval)
  await browser.close()
}
