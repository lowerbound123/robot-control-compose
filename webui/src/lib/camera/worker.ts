import { MjpegParser, multipartBoundary } from './mjpeg'
import { FPS, FrameTimeline } from './timeline'
import { RecordingStorage, type StoredPacket } from './storage'
import { exportRecording } from './mux'
import type { CameraWorkerInput, CameraWorkerOutput, RecordingStatus } from './types'

const send = (message: CameraWorkerOutput, transfer: Transferable[] = []) => postMessage(message, { transfer })
const clock = () => performance.timeOrigin + performance.now()
const timeline = new FrameTimeline<ImageBitmap>(frame => frame.close())
const status: RecordingStatus = { frames: 0, bytes: 0, pendingFrames: 0, error: '', streamError: '', exporting: false, received: 0 }
let stopped = false, previewEnabled = true, previewPending = false
let latest: { blob: Blob; sequence: number | null } | undefined
let encoder: VideoEncoder | undefined, storage: RecordingStorage | undefined, releaseLock: (() => void) | undefined
let metadata: EncodedVideoChunkMetadata = { decoderConfig: { codec: 'vp8', codedWidth: 640, codedHeight: 480 } }
let outputCount = 0, pending: StoredPacket[] = [], writing: Promise<void> | undefined
let controller: AbortController | undefined, timer: ReturnType<typeof setInterval> | undefined
let exportCutoff: number | undefined, lastReported = 0
const errorText = (error: unknown) => error instanceof Error ? error.message : String(error)

function report(force = false) {
  if (!force && clock() - lastReported < 500) return
  lastReported = clock()
  status.pendingFrames = Math.max(0, timeline.frameCount(clock()) - status.frames)
  send({ type: 'status', status: { ...status } })
}
function fail(error: unknown) {
  if (!status.error) status.error = `录像已停止：${errorText(error)}；已保存部分仍可导出。`
  timeline.close()
  report(true)
}
function commit() {
  if (writing || !pending.length || !storage || status.error) return
  const batch = pending; pending = []
  writing = storage.append(batch).then(() => {
    status.frames += batch.length
    status.bytes += batch.reduce((sum, packet) => sum + packet.data.byteLength, 0)
  }).catch(fail).finally(() => { writing = undefined; report() })
}
function pump() {
  if (stopped || status.error || !encoder || encoder.state !== 'configured') return
  try {
    // Backpressure bounds JS packets and native encoder work, including background catch-up.
    if (encoder.encodeQueueSize + pending.length < 30 && !writing) {
      const slots = timeline.take(Math.min(clock(), exportCutoff ?? Infinity), Math.max(0, 8 - encoder.encodeQueueSize))
      for (const slot of slots) {
        const frame = new VideoFrame(slot.frame, { timestamp: slot.timestamp, duration: slot.duration })
        try { encoder.encode(frame, { keyFrame: slot.index % 60 === 0 }) } finally { frame.close() }
      }
    }
    if (pending.length >= 6) commit()
    report()
  } catch (error) { fail(error) }
}
async function preview() {
  if (!previewEnabled || previewPending || !latest || stopped) return
  previewPending = true
  try {
    const source = latest, bitmap = await createImageBitmap(source.blob)
    if (stopped || !previewEnabled) { bitmap.close(); previewPending = false; return }
    send({ type: 'frame', bitmap, sequence: source.sequence }, [bitmap])
  } catch { previewPending = false }
}
async function initialize(session: string, camera: string) {
  if (typeof VideoEncoder === 'undefined' || !navigator.locks) throw Error('当前浏览器不支持录像，请使用桌面 Chrome／Edge 的 localhost 或 HTTPS 页面')
  const config: VideoEncoderConfig = { codec: 'vp8', width: 640, height: 480, bitrate: 2_000_000, framerate: FPS, latencyMode: 'realtime' }
  if (!(await VideoEncoder.isConfigSupported(config)).supported) throw Error('当前浏览器不支持 VP8 视频编码')
  await new Promise<void>((resolve, reject) => {
    void navigator.locks.request(RecordingStorage.lockName(session, camera), async () => {
      storage = await RecordingStorage.open(session, camera)
      encoder = new VideoEncoder({ error: fail, output(chunk, meta) {
        if (stopped || status.error) return
        const index = Math.round(chunk.timestamp * FPS / 1e6)
        if (index !== outputCount) { fail(Error('编码器未返回连续的 30 fps 帧')); return }
        outputCount++
        const data = new Uint8Array(chunk.byteLength); chunk.copyTo(data)
        if (meta?.decoderConfig) metadata = meta
        pending.push({ index, data, type: chunk.type, timestamp: chunk.timestamp, duration: chunk.duration ?? Math.round(1e6 / FPS) })
        if (pending.length >= 6) commit()
      } })
      encoder.configure(config)
      const held = new Promise<void>(done => { releaseLock = done })
      resolve()
      await held
      storage.close()
    }).catch(reject)
  })
  // A live document holds each DB's Web Lock. Never delete another tab's active cache.
  void RecordingStorage.cleanInactive().catch(() => {})
}
async function consume(url: string) {
  while (!stopped) {
    controller = new AbortController()
    try {
      const response = await fetch(url, { signal: controller.signal, cache: 'no-store', credentials: 'same-origin' })
      if (!response.ok || !response.body) throw Error(`图像连接失败（HTTP ${response.status}）`)
      const parser = new MjpegParser(multipartBoundary(response.headers.get('content-type') || ''))
      const reader = response.body.getReader()
      try {
        for (;;) {
          const { done, value } = await reader.read()
          if (done || stopped) { parser.finish(); break }
          for (const frame of parser.push(value)) {
            const at = clock(), blob = new Blob([frame.jpeg], { type: 'image/jpeg' })
            latest = { blob, sequence: frame.sequence }
            void preview()
            if (!status.error && encoder) {
              const bitmap = await createImageBitmap(blob)
              if (stopped || status.error) bitmap.close()
              else {
                try { timeline.receive(bitmap, at); pump() }
                catch (error) { bitmap.close(); fail(error) }
              }
            }
            status.received++
            status.streamError = ''
            report()
          }
        }
      } finally { await reader.cancel().catch(() => {}); reader.releaseLock() }
      if (!stopped) throw Error('图像连接中断，正在重连')
    } catch (error) {
      if (stopped) break
      status.streamError = `${errorText(error)}，正在重连`
      report(true)
    } finally { controller.abort() }
    if (!stopped) await new Promise(resolve => setTimeout(resolve, 2000))
  }
}
async function download(id: number, cutoff: number) {
  if (status.exporting) { send({ type: 'export', id, error: '正在导出，请稍候' }); return }
  status.exporting = true
  exportCutoff = cutoff
  report(true)
  try {
    if (!storage || timeline.start === undefined) throw Error('尚无可导出的录像帧')
    const target = timeline.frameCount(cutoff)
    while (!status.error && timeline.nextIndex < target && !stopped) {
      pump()
      await new Promise(resolve => setTimeout(resolve, 10))
    }
    if (stopped) return
    if (!status.error) {
      await encoder!.flush()
      while (writing || pending.length) {
        await writing
        if (status.error) break
        commit()
      }
    }
    // Continue live recording while a separate muxer reads the immutable snapshot prefix.
    exportCutoff = undefined
    pump()
    const count = Math.min(target, status.frames)
    const blob = await exportRecording(storage, count, metadata)
    send({ type: 'export', id, blob, partial: count < target || !!status.error })
  } catch (error) { send({ type: 'export', id, error: `视频导出失败：${errorText(error)}` }) }
  finally { exportCutoff = undefined; status.exporting = false; report(true) }
}
onmessage = (event: MessageEvent<CameraWorkerInput>) => {
  const message = event.data
  if (message.type === 'start') {
    void (async () => {
      try { await initialize(message.session, message.camera) } catch (error) { fail(error) }
      if (stopped) { releaseLock?.(); return }
      timer = setInterval(pump, 15)
      void consume(message.url)
    })()
  } else if (message.type === 'ack') previewPending = false
  else if (message.type === 'preview') { previewEnabled = message.enabled; if (previewEnabled) void preview() }
  else if (message.type === 'export') void download(message.id, message.cutoff)
  else if (message.type === 'stop') {
    stopped = true
    controller?.abort(); clearInterval(timer)
    if (encoder && encoder.state !== 'closed') encoder.close()
    timeline.close()
    void (writing ?? Promise.resolve()).finally(() => releaseLock?.())
  }
}
