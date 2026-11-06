import { describe, expect, it } from 'vitest'
import { PoseHistory, reducePoints, type StateFrame } from '@/lib/pose/history'
import { PoseWindow, channels } from '@/lib/pose/window'

const frame = (sequence: number, at: number, run = 'a'): StateFrame => ({
  run_id: run, sequence, sampled_monotonic: at / 1000, error: null,
  state: { qpos: Array.from({ length: 14 }, (_, i) => i), eef: Array.from({ length: 14 }, (_, i) => 100 + i),
    sequences: { left: sequence, right: sequence }, timestamps: { left: 10, right: 11 }, feedback_age_s: { left: 0, right: 0 } },
})

describe('session history', () => {
  it('retains every received sample beyond a minute and selects both arms correctly', () => {
    const history = new PoseHistory()
    for (let i = 0; i <= 10000; i++) history.receive(frame(i + 1, i * 20), i * 20)
    expect(history.count).toBe(10001)
    expect(history.slice('joints', 0, 0, 60000).length).toBe(3001)
    expect(history.slice('joints', 13, 0, 60000)[0].value).toBe(13)
    expect(history.slice('eef', 7, 140000, 200000)[0].value).toBe(107)
    expect(history.slice('joints', 0, 0, 100)[0].at).toBe(0)
  })
  it('marks packet loss, disconnect, invalid feedback and RC restart as gaps', () => {
    const history = new PoseHistory()
    history.receive(frame(1, 0), 0)
    history.receive(frame(2, 20), 20)
    history.receive(frame(4, 60), 60)
    history.disconnect()
    history.receive(frame(5, 80), 80)
    history.receive({ ...frame(6, 100), state: null, error: 'stale' }, 100)
    history.receive(frame(7, 120), 120)
    history.receive(frame(1, 0, 'b'), 140)
    const values = history.slice('joints', 0, 0, 1000)
    expect(values.map(value => value.breakBefore)).toEqual([true, false, true, true, true, true])
    expect(values.map(value => value.at)).toEqual([0, 20, 60, 80, 120, 140])
    expect(history.count).toBe(6)
  })
  it('does not accept malformed data or silently reuse an old sample', () => {
    const history = new PoseHistory()
    const invalid = frame(1, 0)
    invalid.state!.qpos[3] = NaN
    expect(() => history.receive(invalid, 0)).toThrow()
    history.receive(frame(2, 20), 20)
    expect(history.count).toBe(1)
    expect(history.slice('joints', 0, 0, 100)[0].breakBefore).toBe(true)
  })
  it('preserves peaks and gap boundaries when reducing a dense viewport', () => {
    const points = [
      { at: 0, value: 0, breakBefore: true }, { at: 1, value: 9, breakBefore: false },
      { at: 2, value: -8, breakBefore: false }, { at: 3, value: 0, breakBefore: false },
      { at: 4, value: 4, breakBefore: true }, { at: 5, value: 1, breakBefore: false },
    ]
    const reduced = reducePoints(points, 0, 60_000, 180)
    expect(reduced.map(p => p.value)).toEqual([0, 9, -8, 0, 4, 1])
    expect(reduced.filter(p => p.breakBefore).map(p => p.at)).toEqual([0, 4])
  })
  it('keeps the adjacent samples needed to clip a line crossing either viewport edge', () => {
    const history = new PoseHistory()
    history.receive(frame(1, 0), 0)
    history.receive(frame(2, 20), 20)
    history.receive(frame(3, 40), 40)
    expect(history.slice('joints', 0, 10, 30).map(p => p.at)).toEqual([0, 20, 40])
  })
  it('queries only a minute after two hours without losing early history', () => {
    const history = new PoseHistory()
    for (let i = 0; i <= 360000; i++) history.receive(frame(i + 1, i * 20), i * 20)
    expect(history.count).toBe(360001)
    expect(history.slice('joints', 0, 7140000, 7200000)).toHaveLength(3001)
    expect(history.slice('eef', 13, 0, 60000)).toHaveLength(3001)
    expect(reducePoints(history.slice('joints', 0, 7140000, 7200000), 7140000, 7200000, 180).length).toBeLessThan(725)
  })
})

describe('independent 60 second viewports', () => {
  it('fills from left then follows the latest point', () => {
    const window = new PoseWindow()
    expect(window.range(0, 10000)).toEqual([0, 60000])
    expect(window.range(0, 90000)).toEqual([30000, 90000])
  })
  it('holds history steady, clamps to start and resumes only at latest', () => {
    const a = new PoseWindow(), b = new PoseWindow()
    a.pan(-10000, 0, 90000)
    expect(a.following).toBe(false)
    expect(a.range(0, 100000)).toEqual([20000, 80000])
    expect(b.range(0, 100000)).toEqual([40000, 100000])
    a.pan(-100000, 0, 100000)
    expect(a.range(0, 100000)).toEqual([0, 60000])
    a.pan(100000, 0, 100000)
    expect(a.following).toBe(true)
    expect(a.range(0, 110000)).toEqual([50000, 110000])
  })
  it('uses radians, metres and raw gripper bounds for the correct dimensions', () => {
    expect(channels('joints').map(c => c.label)).toEqual(['J1', 'J2', 'J3', 'J4', 'J5', 'J6', '夹爪'])
    expect(channels('eef')[0].range).toEqual([-1, 1])
    expect(channels('eef')[3].range).toEqual([-Math.PI, Math.PI])
    expect(channels('joints')[6].range).toEqual([-6, 1])
  })
})
