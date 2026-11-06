import type { PoseMode } from './window'

export type Arm = 'left' | 'right'
export type StateFrame = {
  run_id: string; sequence: number; sampled_monotonic: number; error: string | null
  state: null | { qpos: number[]; eef: number[]; sequences: Record<Arm, number>;
    timestamps: Record<Arm, number>; feedback_age_s: Record<Arm, number> }
}
export type Point = { at: number; value: number; breakBefore: boolean }
export type PoseStatus = { count: number; first: number | null; latest: number | null;
  values: { joints: number[]; eef: number[] } | null; error: string; connected: boolean }

const CHUNK = 2048, STRIDE = 30
const finite = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value)
export function validateFrame(value: unknown): asserts value is StateFrame {
  if (!value || typeof value !== 'object') throw Error('状态流数据格式错误')
  const frame = value as StateFrame
  if (typeof frame.run_id !== 'string' || !frame.run_id || !Number.isSafeInteger(frame.sequence) || frame.sequence < 1
    || !finite(frame.sampled_monotonic) || (frame.error !== null && typeof frame.error !== 'string')) throw Error('状态流数据格式错误')
  if (frame.state === null && typeof frame.error === 'string') return
  const state = frame.state
  if (!state || ![state.qpos, state.eef].every(v => Array.isArray(v) && v.length === 14 && v.every(finite))
    || !['left', 'right'].every(arm => {
      const side = arm as Arm
      return Number.isSafeInteger(state.sequences?.[side]) && state.sequences[side] >= 0
        && finite(state.timestamps?.[side]) && finite(state.feedback_age_s?.[side]) && state.feedback_age_s[side] >= 0
    })) throw Error('状态流位姿数据格式错误')
}

/** Append-only, packed session history. No Vue proxies or per-sample object retention. */
export class PoseHistory {
  private chunks: Float64Array[] = []
  count = 0
  first: number | null = null
  latest: number | null = null
  values: PoseStatus['values'] = null
  error = ''
  private run = ''
  private sequence = 0
  private offset = 0
  private lastSourceTime = -Infinity
  private gap = true
  private armSequences: Record<Arm, number> | null = null

  disconnect(error = '状态流已断开，正在重连') { this.gap = true; this.error = error }

  receive(value: unknown, receivedAt: number) {
    validateFrame(value)
    const frame = value
    const sourceAt = frame.sampled_monotonic * 1000
    if (frame.run_id !== this.run) {
      this.run = frame.run_id
      this.offset = Math.max(receivedAt, (this.latest ?? -Infinity) + .001) - sourceAt
      this.sequence = 0
      this.lastSourceTime = -Infinity
      this.armSequences = null
      this.gap = true
    }
    if (frame.sequence <= this.sequence) return
    if (sourceAt < this.lastSourceTime) throw Error('状态流时间倒退')
    this.gap ||= frame.sequence !== this.sequence + 1 || sourceAt - this.lastSourceTime > 250
    this.sequence = frame.sequence
    this.lastSourceTime = sourceAt
    const at = Math.round((sourceAt + this.offset) * 1000) / 1000
    this.latest = at
    if (!frame.state) { this.disconnect(frame.error || '反馈不可用'); return }
    const state = frame.state
    let mask = this.gap ? 3 : 0
    for (const [index, arm] of (['left', 'right'] as const).entries()) {
      if (this.armSequences && state.sequences[arm] < this.armSequences[arm]) mask |= 1 << index
    }
    const chunkIndex = Math.floor(this.count / CHUNK), offset = this.count % CHUNK * STRIDE
    if (!this.chunks[chunkIndex]) this.chunks.push(new Float64Array(CHUNK * STRIDE))
    const chunk = this.chunks[chunkIndex]
    chunk[offset] = at; chunk[offset + 1] = mask
    chunk.set(state.qpos, offset + 2); chunk.set(state.eef, offset + 16)
    this.count++
    this.first ??= at
    this.values = { joints: state.qpos, eef: state.eef }
    this.armSequences = state.sequences
    this.gap = false; this.error = ''
  }

  private number(index: number, field: number) { return this.chunks[Math.floor(index / CHUNK)][index % CHUNK * STRIDE + field] }
  private lowerBound(at: number) {
    let left = 0, right = this.count
    while (left < right) {
      const middle = Math.floor((left + right) / 2)
      if (this.number(middle, 0) < at) left = middle + 1
      else right = middle
    }
    return left
  }
  slice(mode: PoseMode, channel: number, start: number, end: number): Point[] {
    if (channel < 0 || channel >= 14 || !Number.isInteger(channel)) throw Error('未知位姿通道')
    let first = this.lowerBound(start), last = this.lowerBound(end)
    if (first > 0 && (first === this.count || this.number(first, 0) > start)) first--
    last = Math.min(this.count, last + 1)
    const field = (mode === 'joints' ? 2 : 16) + channel
    const points: Point[] = []
    for (let i = first; i < last; i++) points.push({ at: this.number(i, 0), value: this.number(i, field),
      breakBefore: !!(this.number(i, 1) & (channel < 7 ? 1 : 2)) })
    return points
  }
  status(connected: boolean): PoseStatus {
    return { count: this.count, first: this.first, latest: this.latest, values: this.values, error: this.error, connected }
  }
}

/** Retain extrema and discontinuities in every pixel bucket; raw history stays intact. */
export function reducePoints(points: Point[], start: number, end: number, pixels: number): Point[] {
  const result: Point[] = []
  let bucket: Point[] = [], previousPixel = -Infinity
  const flush = () => {
    if (!bucket.length) return
    let min = 0, max = 0
    for (let i = 1; i < bucket.length; i++) {
      if (bucket[i].value < bucket[min].value) min = i
      if (bucket[i].value > bucket[max].value) max = i
    }
    for (const i of [...new Set([0, min, max, bucket.length - 1])].sort((a, b) => a - b)) result.push(bucket[i])
    bucket = []
  }
  for (const point of points) {
    const pixel = Math.floor((point.at - start) / (end - start) * Math.max(1, pixels))
    if (pixel !== previousPixel || point.breakBefore) flush()
    bucket.push(point); previousPixel = pixel
  }
  flush()
  return result
}
