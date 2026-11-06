export type JpegFrame = { jpeg: Uint8Array<ArrayBuffer>; sequence: number | null }
const MAX_HEADER = 16 * 1024, MAX_FRAME = 2 * 1024 * 1024

/** The existing RC multipart endpoint supplies Content-Length for each JPEG. */
export class MjpegParser {
  private buffer: Uint8Array<ArrayBuffer> = new Uint8Array()
  private length: number | undefined
  private sequence: number | null = null
  constructor(private boundary: string) {
    if (!boundary || boundary.length > 200 || /[\r\n]/.test(boundary)) throw Error('无效的图像流边界')
  }
  push(bytes: Uint8Array): JpegFrame[] {
    const joined = new Uint8Array(this.buffer.length + bytes.length)
    joined.set(this.buffer); joined.set(bytes, this.buffer.length); this.buffer = joined
    const frames: JpegFrame[] = []
    for (;;) {
      if (this.length === undefined) {
        let end = -1
        for (let i = 0; i + 3 < this.buffer.length; i++) {
          if (this.buffer[i] === 13 && this.buffer[i + 1] === 10 && this.buffer[i + 2] === 13 && this.buffer[i + 3] === 10) { end = i; break }
        }
        if (end < 0) { if (this.buffer.length > MAX_HEADER) throw Error('图像流头部过长'); break }
        if (end > MAX_HEADER) throw Error('图像流头部过长')
        const header = new TextDecoder().decode(this.buffer.subarray(0, end)).trim()
        if (!header.startsWith(`--${this.boundary}\r\n`)) throw Error('图像流边界错误')
        const size = /^content-length:\s*(\d+)\s*$/im.exec(header)?.[1]
        this.length = size === undefined ? NaN : Number(size)
        if (!Number.isSafeInteger(this.length) || this.length <= 0 || this.length > MAX_FRAME) throw Error('图像流长度无效')
        this.sequence = Number(/^x-sequence:\s*(\d+)\s*$/im.exec(header)?.[1]) || null
        this.buffer = this.buffer.slice(end + 4)
      }
      if (this.buffer.length < this.length) break
      frames.push({ jpeg: this.buffer.slice(0, this.length), sequence: this.sequence })
      this.buffer = this.buffer.slice(this.length)
      this.length = undefined
    }
    return frames
  }
  finish() {
    const remainder = new TextDecoder().decode(this.buffer).trim()
    if (this.length !== undefined || (remainder && remainder !== `--${this.boundary}--`)) throw Error('图像流提前结束')
  }
}

export function multipartBoundary(contentType: string) {
  if (!/^multipart\/x-mixed-replace\b/i.test(contentType)) throw Error('RC 未返回 MJPEG 图像流')
  const boundary = /boundary\s*=\s*(?:"([^"]+)"|([^;\s]+))/i.exec(contentType)
  if (!boundary) throw Error('图像流缺少边界')
  return boundary[1] || boundary[2]!
}
