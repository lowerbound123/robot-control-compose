export type StoredPacket = { index: number; data: Uint8Array<ArrayBuffer>; type: EncodedVideoChunkType; timestamp: number; duration: number }
const PREFIX = 'rc-webui-video-'
function request<T>(value: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => { value.onsuccess = () => resolve(value.result); value.onerror = () => reject(value.error) })
}
function transactionDone(tx: IDBTransaction) {
  return new Promise<void>((resolve, reject) => { tx.oncomplete = () => resolve(); tx.onabort = tx.onerror = () => reject(tx.error || Error('录像存储失败')) })
}

export class RecordingStorage {
  private constructor(readonly name: string, private db: IDBDatabase) {}
  static async open(session: string, camera: string) {
    const name = `${PREFIX}${session}-${camera}`
    const opening = indexedDB.open(name, 1)
    opening.onupgradeneeded = () => {
      opening.result.createObjectStore('packets', { keyPath: 'index' })
      opening.result.createObjectStore('files')
    }
    return new RecordingStorage(name, await request(opening))
  }
  static lockName(session: string, camera: string) { return `${PREFIX}${session}-${camera}` }
  static async cleanInactive() {
    if (!indexedDB.databases || !navigator.locks) return
    for (const info of await indexedDB.databases()) {
      if (!info.name?.startsWith(PREFIX)) continue
      const name = info.name
      await navigator.locks.request(name, { ifAvailable: true }, async lock => {
        if (!lock) return
        await request(indexedDB.deleteDatabase(name))
      })
    }
  }
  async append(packets: StoredPacket[]) {
    const tx = this.db.transaction('packets', 'readwrite'), done = transactionDone(tx)
    for (const packet of packets) tx.objectStore('packets').add(packet)
    await done
  }
  async read(from: number, through: number) {
    return request(this.db.transaction('packets').objectStore('packets').getAll(IDBKeyRange.bound(from, through))) as Promise<StoredPacket[]>
  }
  async putBlock(key: string, blob: Blob) {
    const tx = this.db.transaction('files', 'readwrite'), done = transactionDone(tx)
    tx.objectStore('files').put(blob, key); await done
  }
  async block(key: string): Promise<Blob | undefined> { return request(this.db.transaction('files').objectStore('files').get(key)) }
  async deleteBlocks(prefix: string) {
    const tx = this.db.transaction('files', 'readwrite'), done = transactionDone(tx)
    tx.objectStore('files').delete(IDBKeyRange.bound(prefix, `${prefix}\uffff`)); await done
  }
  close() { this.db.close() }
}

/** Supports muxer header/index rewrites without accumulating the full file in JS memory. */
export class StoredVideoFile {
  private size = 0
  private readonly blockSize = 1024 * 1024
  private readonly prefix = `export-${crypto.randomUUID()}/`
  constructor(private storage: RecordingStorage) {}
  async write(position: number, data: Uint8Array<ArrayBuffer>) {
    this.size = Math.max(this.size, position + data.byteLength)
    let offset = 0
    while (offset < data.length) {
      const index = Math.floor(position / this.blockSize), within = position % this.blockSize
      const length = Math.min(data.length - offset, this.blockSize - within), key = this.prefix + index
      const old = await this.storage.block(key) ?? new Blob([])
      const padding = Math.max(0, within - old.size)
      const blob = new Blob([old.slice(0, within), new Uint8Array(padding), data.slice(offset, offset + length), old.slice(within + length)])
      await this.storage.putBlock(key, blob)
      offset += length; position += length
    }
  }
  async blob() {
    const parts: Blob[] = []
    for (let i = 0; i < Math.ceil(this.size / this.blockSize); i++) {
      const block = await this.storage.block(this.prefix + i)
      if (!block) throw Error('视频封装缺少数据块')
      parts.push(block)
    }
    return new Blob(parts, { type: 'video/webm' }).slice(0, this.size, 'video/webm')
  }
  dispose() { return this.storage.deleteBlocks(this.prefix) }
}
