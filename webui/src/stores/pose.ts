import { shallowRef } from 'vue'
import { defineStore } from 'pinia'
import type { PoseStatus } from '@/lib/pose/history'
import { PoseWindow, type PoseMode } from '@/lib/pose/window'
import type { ChartRequest, ChartResult, PoseWorkerInput, PoseWorkerOutput } from '@/lib/pose/types'

type Binding = { request: () => ChartRequest | null; draw: (result: ChartResult) => void }
export const usePoseStore = defineStore('pose', () => {
  const status = shallowRef<PoseStatus>({ count: 0, first: null, latest: null, values: null, error: '', connected: false })
  const windows = new Map<string, PoseWindow>(), bindings = new Map<string, Binding>()
  let worker: Worker | undefined, frame = 0, serial = 0, pending = false, running = false
  const key = (mode: PoseMode, channel: number) => `${mode}-${channel}`
  const send = (message: PoseWorkerInput) => worker?.postMessage(message)
  function windowFor(mode: PoseMode, channel: number) {
    const id = key(mode, channel)
    if (!windows.has(id)) windows.set(id, new PoseWindow())
    return windows.get(id)!
  }
  function animate() {
    if (!running) return
    if (!document.hidden && !pending && bindings.size) {
      const charts = [...bindings.values()].map(value => value.request()).filter((value): value is ChartRequest => value !== null)
      if (charts.length) { pending = true; send({ type: 'view', id: ++serial, charts }) }
    }
    frame = requestAnimationFrame(animate)
  }
  function suspend() {
    if (!running) return
    running = false; cancelAnimationFrame(frame); send({ type: 'stop' })
  }
  function start() {
    if (running) return
    running = true
    if (!worker) {
      worker = new Worker(new URL('../lib/pose/worker.ts', import.meta.url), { type: 'module' })
      worker.onmessage = (event: MessageEvent<PoseWorkerOutput>) => {
        const message = event.data
        status.value = message.status
        if (message.type === 'view') {
          pending = false
          for (const chart of message.charts) bindings.get(chart.key)?.draw(chart)
        }
      }
      worker.onerror = () => {
        pending = false
        status.value = { ...status.value, connected: false, error: '位姿处理异常，请刷新页面重新连接' }
        suspend()
      }
      window.addEventListener('pagehide', suspend)
      window.addEventListener('beforeunload', suspend)
      window.addEventListener('pageshow', start)
    }
    pending = false
    send({ type: 'start', url: new URL('/state/stream', location.origin).href })
    frame = requestAnimationFrame(animate)
  }
  function bind(mode: PoseMode, channel: number, binding: Binding | null) {
    const id = key(mode, channel)
    if (binding) bindings.set(id, binding)
    else bindings.delete(id)
  }
  function dispose() {
    suspend(); worker?.terminate(); worker = undefined; bindings.clear()
    window.removeEventListener('pagehide', suspend)
    window.removeEventListener('beforeunload', suspend)
    window.removeEventListener('pageshow', start)
  }
  return { status, windowFor, key, bind, start, dispose }
})
