import { EncodedPacket, EncodedVideoPacketSource, Output, StreamTarget, WebMOutputFormat } from 'mediabunny'
import { RecordingStorage, StoredVideoFile } from './storage'
import { FPS } from './timeline'

export async function exportRecording(storage: RecordingStorage, count: number, metadata: EncodedVideoChunkMetadata) {
  if (!count) throw Error('尚无可导出的录像帧')
  const file = new StoredVideoFile(storage), source = new EncodedVideoPacketSource('vp8')
  const output = new Output({ format: new WebMOutputFormat(), target: new StreamTarget(new WritableStream({
    write: chunk => file.write(chunk.position, chunk.data),
  }), { chunked: true, chunkSize: 1024 * 1024 }) })
  output.addVideoTrack(source, { frameRate: FPS })
  try {
    await output.start()
    for (let first = 0; first < count; first += 120) {
      const last = Math.min(count - 1, first + 119)
      const packets = await storage.read(first, last)
      if (packets.length !== last - first + 1) throw Error('录像缓存不完整，无法导出连续视频')
      for (const packet of packets) await source.add(new EncodedPacket(packet.data, packet.type,
        packet.timestamp / 1e6, packet.duration / 1e6), packet.index === 0 ? metadata : undefined)
    }
    await output.finalize()
    return await file.blob()
  } catch (error) {
    await output.cancel().catch(() => {})
    throw error
  } finally { await file.dispose().catch(() => {}) }
}
