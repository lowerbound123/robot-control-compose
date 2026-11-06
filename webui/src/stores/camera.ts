import { reactive } from 'vue'
import { defineStore } from 'pinia'
import { cameras, type CameraId, type CameraWorkerInput, type CameraWorkerOutput, type RecordingStatus } from '@/lib/camera/types'
import { downloadBlob, fileTime } from '@/lib/download'
import { sessionId } from '@/lib/session-log'

type CameraState = RecordingStatus & { ready: boolean; downloadError: string; sequence: number | null }
const initialState = (): CameraState => ({ frames: 0, bytes: 0, pendingFrames: 0, error: '', streamError: '', exporting: false, received: 0,
  ready: false, downloadError: '', sequence: null })
export const useCameraStore = defineStore('camera', () => {
  const state = reactive(Object.fromEntries(cameras.map(({ camera }) => [camera, initialState()])) as Record<CameraId, CameraState>)
  const workers = new Map<CameraId, Worker>(), targets = new Map<CameraId, HTMLCanvasElement>()
  const buffers = new Map<CameraId, HTMLCanvasElement>()
  let started = false, nextExport = 0
  const send = (camera: CameraId, message: CameraWorkerInput) => workers.get(camera)?.postMessage(message)
  function setPreview() {
    for (const { camera } of cameras) send(camera, { type: 'preview', enabled: targets.has(camera) && !document.hidden })
  }
  function start() {
    if (started) return
    started = true
    const session = sessionId()
    for (const { camera } of cameras) {
      Object.assign(state[camera], initialState())
      const worker = new Worker(new URL('../lib/camera/worker.ts', import.meta.url), { type: 'module' })
      const buffer = document.createElement('canvas'); buffer.width = 640; buffer.height = 480
      buffers.set(camera, buffer); workers.set(camera, worker)
      worker.onmessage = (event: MessageEvent<CameraWorkerOutput>) => {
        const message = event.data, current = state[camera]
        if (message.type === 'status') Object.assign(current, message.status)
        else if (message.type === 'frame') {
          try {
            if (!started) return
            buffer.getContext('2d')!.drawImage(message.bitmap, 0, 0, 640, 480)
            const target = targets.get(camera)
            if (target) target.getContext('2d')!.drawImage(buffer, 0, 0)
            current.ready = true; current.sequence = message.sequence
          } finally { message.bitmap.close(); send(camera, { type: 'ack' }) }
        } else if (message.type === 'export') {
          current.exporting = false
          current.downloadError = message.error || ''
          if (message.blob) {
            try { downloadBlob(message.blob, `robot-control-${camera}-${fileTime()}${message.partial ? '-partial' : ''}.webm`) }
            catch { current.downloadError = '视频下载失败，请重试；录像记录仍然保留。' }
          }
        }
      }
      worker.onerror = () => { state[camera].error = '相机处理异常，请刷新页面重新连接。'; state[camera].exporting = false }
      send(camera, { type: 'start', session, camera, url: new URL(`/preview/${camera}.mjpg`, location.origin).href })
    }
    setPreview()
    document.addEventListener('visibilitychange', setPreview)
    window.addEventListener('pagehide', stop)
    // Release long HTTP/1 streams before navigation requests need a connection slot.
    window.addEventListener('beforeunload', stop)
    window.addEventListener('pageshow', start)
  }
  function bind(camera: CameraId, element: HTMLCanvasElement | null) {
    if (element) {
      targets.set(camera, element)
      if (state[camera].ready && buffers.has(camera)) element.getContext('2d')!.drawImage(buffers.get(camera)!, 0, 0)
    } else targets.delete(camera)
    setPreview()
  }
  function download(camera: CameraId) {
    if (!state[camera].frames || state[camera].exporting) return
    state[camera].exporting = true; state[camera].downloadError = ''
    send(camera, { type: 'export', id: ++nextExport, cutoff: performance.timeOrigin + performance.now() })
  }
  function stop() {
    if (!started) return
    started = false
    document.removeEventListener('visibilitychange', setPreview)
    window.removeEventListener('pagehide', stop)
    window.removeEventListener('beforeunload', stop)
    for (const [camera, worker] of workers) { send(camera, { type: 'stop' }); worker.terminate() }
    workers.clear(); buffers.clear()
  }
  function dispose() { stop(); window.removeEventListener('pageshow', start) }
  return { state, start, stop, dispose, bind, download }
})
