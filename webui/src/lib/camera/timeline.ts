export const FPS = 30
export const frameTime = (index: number) => Math.round(index * 1_000_000 / FPS)
export type TimedFrame<T> = { frame: T; index: number; timestamp: number; duration: number }

/** Bounded source changes, not one allocation per missing video frame. Times are milliseconds. */
export class FrameTimeline<T> {
  private samples: { frame: T; at: number }[] = []
  private retired: T[] = []
  start: number | undefined
  nextIndex = 0
  constructor(private release: (frame: T) => void = () => {}, private capacity = 60) {}
  receive(frame: T, at: number) {
    if (this.start === undefined) this.start = at
    const previous = this.samples.at(-1)
    if (previous && at < previous.at) throw Error('图像时间顺序错误')
    const firstSlot = (time: number) => Math.ceil((time - this.start!) * FPS / 1000 - 1e-7)
    // A network burst may contain many JPEGs before the same next video slot.
    // Only the latest of these can be sampled; keep the earlier time interval intact.
    if (previous && firstSlot(previous.at) === firstSlot(at)) {
      this.release(previous.frame)
      this.samples[this.samples.length - 1] = { frame, at }
      return
    }
    if (this.samples.length >= this.capacity) throw Error('录像编码落后，已停止累计；已有内容仍可导出。')
    this.samples.push({ frame, at })
  }
  frameCount(until: number) {
    return this.start === undefined ? 0 : Math.max(0, Math.ceil((until - this.start) * FPS / 1000 - 1e-7))
  }
  take(until: number, limit: number): TimedFrame<T>[] {
    // A returned batch remains valid until the next take, so callers can encode it synchronously.
    for (const frame of this.retired) this.release(frame)
    this.retired = []
    const count = this.frameCount(until), result: TimedFrame<T>[] = []
    while (this.samples.length && this.nextIndex < count && result.length < limit) {
      const index = this.nextIndex++, timestamp = frameTime(index)
      const at = this.start! + timestamp / 1000
      while (this.samples.length > 1 && this.samples[1]!.at <= at + 0.0005) this.retired.push(this.samples.shift()!.frame)
      result.push({ frame: this.samples[0]!.frame, index, timestamp, duration: frameTime(index + 1) - timestamp })
    }
    return result
  }
  close() {
    for (const { frame } of this.samples) this.release(frame)
    for (const frame of this.retired) this.release(frame)
    this.samples = []; this.retired = []
  }
}
