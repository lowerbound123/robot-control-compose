import type { Point, PoseStatus } from './history'
import type { PoseMode } from './window'

export type ChartRequest = { key: string; mode: PoseMode; channel: number; start: number; end: number; pixels: number }
export type ChartResult = ChartRequest & { points: Point[] }
export type PoseWorkerInput = { type: 'start'; url: string } | { type: 'stop' }
  | { type: 'view'; id: number; charts: ChartRequest[] }
export type PoseWorkerOutput = { type: 'status'; status: PoseStatus }
  | { type: 'view'; id: number; charts: ChartResult[]; status: PoseStatus }
