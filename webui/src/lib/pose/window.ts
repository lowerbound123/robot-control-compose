export type PoseMode = 'joints' | 'eef'
export const WINDOW_MS = 60_000
export type Channel = { label: string; unit: string; range: [number, number] }

export function channels(mode: PoseMode): Channel[] {
  const labels = mode === 'joints' ? ['J1', 'J2', 'J3', 'J4', 'J5', 'J6'] : ['X', 'Y', 'Z', 'Roll', 'Pitch', 'Yaw']
  return [...labels.map((label, index): Channel => ({ label,
    unit: mode === 'eef' && index < 3 ? 'm' : 'rad',
    range: mode === 'eef' && index < 3 ? [-1, 1] : [-Math.PI, Math.PI],
  })), { label: '夹爪', unit: '原始值', range: [-6, 1] }]
}

export class PoseWindow {
  following = true
  private start = 0
  range(first: number, latest: number): [number, number] {
    const start = this.following ? Math.max(first, latest - WINDOW_MS) : this.start
    return [start, start + WINDOW_MS]
  }
  pan(deltaMs: number, first: number, latest: number) {
    const max = Math.max(first, latest - WINDOW_MS)
    this.start = Math.max(first, Math.min(max, this.range(first, latest)[0] + deltaMs))
    this.following = this.start >= max - .5
  }
  live() { this.following = true }
}
