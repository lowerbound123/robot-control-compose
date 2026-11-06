import { expect, it } from 'vitest'
import { MjpegParser } from '../src/lib/camera/mjpeg'
import { FrameTimeline, frameTime } from '../src/lib/camera/timeline'

const encode = new TextEncoder()
function part(bytes: number[], sequence: number) {
  const header = encode.encode(`--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ${bytes.length}\r\nX-Sequence: ${sequence}\r\n\r\n`)
  return new Uint8Array([...header, ...bytes, 13, 10])
}
it('parses every network split including JPEG bytes containing boundary text', () => {
  const first = [255, 216, ...encode.encode('\r\n--frame'), 255, 217], second = [255, 216, 4, 255, 217]
  const wire = new Uint8Array([...part(first, 1), ...part(second, 2)])
  for (let split = 1; split <= wire.length; split++) {
    const parser = new MjpegParser('frame'), frames = []
    for (let at = 0; at < wire.length; at += split) frames.push(...parser.push(wire.slice(at, at + split)))
    expect(frames.map(frame => [...frame.jpeg])).toEqual([first, second])
    expect(frames.map(frame => frame.sequence)).toEqual([1, 2])
    parser.finish()
  }
})
it('bounds malformed headers, lengths and incomplete frames', () => {
  expect(() => new MjpegParser('frame').push(encode.encode('--frame\r\nContent-Length: 99999999\r\n\r\n'))).toThrow()
  expect(() => new MjpegParser('frame').push(encode.encode('--frame\r\n' + 'x'.repeat(17000)))).toThrow()
  const parser = new MjpegParser('frame')
  parser.push(part([1, 2, 3], 1).slice(0, -3))
  expect(() => parser.finish()).toThrow()
})
it('uses exact 30fps timestamps without accumulating fractional drift', () => {
  expect(frameTime(30)).toBe(1_000_000)
  expect(frameTime(18_000)).toBe(600_000_000)
  const frames = new FrameTimeline<string>()
  frames.receive('A', 1000)
  const output = frames.take(2000, 31)
  expect(output).toHaveLength(30)
  expect(output[0]).toMatchObject({ frame: 'A', index: 0, timestamp: 0 })
  expect(output.at(-1)?.timestamp).toBe(frameTime(29))
})
it('fills disconnect/background gaps with the old frame before showing new arrivals', () => {
  const frames = new FrameTimeline<string>()
  frames.receive('old', 0)
  frames.receive('new', 2000)
  const before = frames.take(2100, 100)
  expect(before).toHaveLength(63)
  expect(before.slice(0, 60).every(frame => frame.frame === 'old')).toBe(true)
  expect(before.slice(60).every(frame => frame.frame === 'new')).toBe(true)
  expect(frames.take(2100, 100)).toHaveLength(0)
})
it('bounds work per pump and releases frames only after their last encoded slot', () => {
  const released: string[] = [], frames = new FrameTimeline<string>(value => released.push(value))
  frames.receive('A', 0); frames.receive('B', 40)
  expect(frames.take(1000, 2).map(value => value.frame)).toEqual(['A', 'A'])
  expect(released).toEqual([])
  expect(frames.take(1000, 2).map(value => value.frame)).toEqual(['B', 'B'])
  expect(released).toEqual([])
  frames.take(1000, 2)
  expect(released).toEqual(['A'])
  frames.close()
  expect(released).toEqual(['A', 'B'])
})
it('fails on excessive capture backlog rather than silently losing early video', () => {
  const frames = new FrameTimeline<number>(undefined, 3)
  frames.receive(1, 0); frames.receive(2, 34); frames.receive(3, 68)
  expect(() => frames.receive(4, 102)).toThrow(/落后/)
  expect(frames.take(100, 10).map(frame => frame.frame)).toEqual([1, 1, 2])
})

it('coalesces a burst within one video slot to bound buffered frames after browser suspension', () => {
  const released: number[] = [], frames = new FrameTimeline<number>(frame => released.push(frame), 3)
  frames.receive(0, 0)
  for (let i = 1; i <= 200; i++) frames.receive(i, 2000 + i / 100)
  const output = frames.take(2100, 100)
  expect(output.slice(0, 61).every(frame => frame.frame === 0)).toBe(true)
  expect(output.slice(61).every(frame => frame.frame === 200)).toBe(true)
  expect(released).toHaveLength(199)
})
