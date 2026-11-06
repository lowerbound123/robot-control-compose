export const cameras = [
  { camera: 'left_wrist', label: '左腕' },
  { camera: 'head', label: '头部' },
  { camera: 'right_wrist', label: '右腕' },
] as const
export type CameraId = typeof cameras[number]['camera']
export type RecordingStatus = {
  frames: number; bytes: number; pendingFrames: number; error: string; streamError: string; exporting: boolean; received: number;
}
export type CameraWorkerInput =
  | { type: 'start'; camera: CameraId; session: string; url: string }
  | { type: 'preview'; enabled: boolean }
  | { type: 'ack' }
  | { type: 'export'; id: number; cutoff: number }
  | { type: 'stop' }
export type CameraWorkerOutput =
  | { type: 'frame'; bitmap: ImageBitmap; sequence: number | null }
  | { type: 'status'; status: RecordingStatus }
  | { type: 'export'; id: number; blob?: Blob; error?: string; partial?: boolean }
