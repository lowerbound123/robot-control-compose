import { expect, it } from 'vitest'
import { StoredVideoFile, type RecordingStorage } from '../src/lib/camera/storage'

function memory() {
  const blocks = new Map<string, Blob>()
  const storage = {
    block: async (key: string) => blocks.get(key),
    putBlock: async (key: string, value: Blob) => { blocks.set(key, value) },
    deleteBlocks: async (prefix: string) => { for (const key of blocks.keys()) if (key.startsWith(prefix)) blocks.delete(key) },
  }
  return { blocks, storage: storage as unknown as RecordingStorage }
}
it('preserves file contents across block boundaries and later muxer header rewrites', async () => {
  const { storage, blocks } = memory(), file = new StoredVideoFile(storage), size = 1024 * 1024
  const expected = new Uint8Array(size * 2 + 73).fill(53)
  await file.write(0, expected)
  await file.write(size - 2, new Uint8Array([1, 2, 3, 4, 5]))
  expected.set([1, 2, 3, 4, 5], size - 2)
  await file.write(0, new Uint8Array([7, 8, 9]))
  expected.set([7, 8, 9], 0)
  expect(new Uint8Array(await (await file.blob()).arrayBuffer())).toEqual(expected)
  await file.dispose()
  expect(blocks.size).toBe(0)
})
it('surfaces failed export writes without altering previously stored recording packets', async () => {
  const { storage, blocks } = memory(), file = new StoredVideoFile(storage)
  await file.write(0, new Uint8Array([1, 2, 3]))
  storage.putBlock = async () => { throw new DOMException('full', 'QuotaExceededError') }
  await expect(file.write(0, new Uint8Array([4]))).rejects.toThrow('full')
  expect(new Uint8Array(await (await file.blob()).arrayBuffer())).toEqual(new Uint8Array([1, 2, 3]))
  await file.dispose()
  expect(blocks.size).toBe(0)
})
