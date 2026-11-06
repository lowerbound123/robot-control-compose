import type { ChartResult } from './types'
import type { Channel } from './window'

const timeLabel = (at: number) => new Date(at).toLocaleTimeString('zh-CN', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
const axisValue = (value: number) => Math.abs(value) === Math.PI ? (value < 0 ? '−π' : 'π') : String(Number(value.toFixed(2)))

export function drawChart(canvas: HTMLCanvasElement, chart: ChartResult, channel: Channel, current: number | undefined) {
  const size = canvas.clientWidth, dpr = window.devicePixelRatio || 1
  if (!size) return
  const resolution = Math.round(size * dpr)
  if (canvas.width !== resolution || canvas.height !== resolution) canvas.width = canvas.height = resolution
  const context = canvas.getContext('2d')!
  context.setTransform(dpr, 0, 0, dpr, 0, 0)
  context.clearRect(0, 0, size, size)
  const left = 34, right = size - 10, top = 12, bottom = size - 27
  const [min, max] = channel.range
  const x = (at: number) => left + (at - chart.start) / (chart.end - chart.start) * (right - left)
  const y = (value: number) => bottom - (value - min) / (max - min) * (bottom - top)
  context.font = '10px -apple-system, BlinkMacSystemFont, sans-serif'
  context.lineWidth = 1
  for (const value of [min, (min + max) / 2, max]) {
    context.strokeStyle = '#e8edf2'; context.beginPath(); context.moveTo(left, y(value)); context.lineTo(right, y(value)); context.stroke()
    context.fillStyle = '#7b8795'; context.textAlign = 'right'; context.textBaseline = 'middle'
    context.fillText(axisValue(value), left - 6, y(value))
  }
  context.fillStyle = '#7b8795'; context.textBaseline = 'alphabetic'; context.textAlign = 'left'
  context.fillText(timeLabel(chart.start), left, size - 8)
  context.textAlign = 'right'; context.fillText(timeLabel(chart.end), right, size - 8)
  if (size > 300) {
    context.textAlign = 'center'; context.fillText(timeLabel((chart.start + chart.end) / 2), (left + right) / 2, size - 8)
  }
  context.save()
  context.beginPath(); context.rect(left, top - 1, right - left, bottom - top + 2); context.clip()
  if (current !== undefined) {
    context.strokeStyle = 'rgba(105, 115, 128, .32)'; context.lineWidth = 1
    context.beginPath(); context.moveTo(left, y(current)); context.lineTo(right, y(current)); context.stroke()
  }
  context.strokeStyle = '#456f99'; context.lineWidth = 1.5; context.lineJoin = 'round'
  context.beginPath()
  for (let index = 0; index < chart.points.length; index++) {
    const point = chart.points[index]
    if (index === 0 || point.breakBefore) context.moveTo(x(point.at), y(point.value))
    else context.lineTo(x(point.at), y(point.value))
  }
  context.stroke()
  const last = chart.points.at(-1)
  if (last) {
    context.fillStyle = '#456f99'; context.beginPath(); context.arc(x(last.at), y(last.value), 2.5, 0, 2 * Math.PI); context.fill()
  }
  context.restore()
}
