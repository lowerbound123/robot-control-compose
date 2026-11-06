/** Share only network reception across same-origin tabs, never their histories or recordings.
 * Four long HTTP/1 streams must not become eight when a second tab opens.
 * Each follower has one unacknowledged packet; slow/frozen followers cannot grow a queue.
 */
type Packet<T> = { type: 'packet'; from: string; to: string; sequence: number; epoch: number; value?: T; error?: string }
type Message<T> = Packet<T> | { type: 'join' | 'leave' | 'ack'; from: string; sequence?: number }
type Peer = { seen: number; pending: boolean; sent: number }
type Producer<T> = (signal: AbortSignal, publish: (value: T) => Promise<void>, fail: (error: string) => void) => Promise<void>

export class SharedFeed<T> {
  private id = crypto.randomUUID()
  private controller = new AbortController()
  private channel?: BroadcastChannel
  private timer?: ReturnType<typeof setInterval>
  private leader = false
  private peers = new Map<string, Peer>()
  private latest?: Packet<T>
  private sequence = 0
  private epoch = 0
  private seenEpoch?: string
  private receiving = false
  private queued?: Packet<T>

  constructor(name: string, producer: Producer<T>, private receive: (value: T) => void | Promise<void>, private failure: (error: string) => void) {
    const run = async () => {
      this.leader = true
      this.post({ type: 'join', from: this.id })
      await producer(this.controller.signal, value => this.publish({ value }), error => { this.epoch++; void this.publish({ error }) })
    }
    if (typeof BroadcastChannel === 'undefined' || !navigator.locks) {
      void run().catch(error => { if (!this.controller.signal.aborted) failure(String(error)) })
      return
    }
    this.channel = new BroadcastChannel('rc-feed-v1:' + name)
    this.channel.onmessage = (event: MessageEvent<Message<T>>) => {
      const message = event.data
      if (message.type === 'packet') {
        if (message.to === this.id) void this.deliver(message)
      } else if (message.type === 'leave') this.peers.delete(message.from)
      else if (message.type === 'join') {
        if (this.leader) {
          if (!this.peers.has(message.from)) this.peers.set(message.from, { seen: Date.now(), pending: false, sent: 0 })
          this.peers.get(message.from)!.seen = Date.now()
          this.sendLatest(message.from)
        } else this.post({ type: 'ack', from: this.id }) // announce to a newly elected producer
      } else if (this.leader) {
        let peer = this.peers.get(message.from)
        if (!peer) { peer = { seen: Date.now(), pending: false, sent: 0 }; this.peers.set(message.from, peer) }
        peer.seen = Date.now()
        if (message.sequence === peer.sent) peer.pending = false
        this.sendLatest(message.from)
      }
    }
    this.timer = setInterval(() => {
      this.post({ type: 'join', from: this.id })
      for (const [id, peer] of this.peers) if (Date.now() - peer.seen > 10_000) this.peers.delete(id)
    }, 1000)
    this.post({ type: 'join', from: this.id })
    void navigator.locks.request('rc-feed-v1:' + name, { signal: this.controller.signal }, run)
      .catch(error => { if (!this.controller.signal.aborted) failure(String(error)) })
  }

  private post(message: Message<T>) { if (!this.controller.signal.aborted) this.channel?.postMessage(message) }
  private sendLatest(id: string) {
    const peer = this.peers.get(id)
    if (!peer || peer.pending || !this.latest || peer.sent === this.latest.sequence) return
    peer.pending = true; peer.sent = this.latest.sequence
    this.post({ ...this.latest, to: id })
  }
  private async publish(value: { value?: T; error?: string }) {
    if (this.controller.signal.aborted) return
    const packet: Packet<T> = { type: 'packet', from: this.id, to: this.id, sequence: ++this.sequence, epoch: this.epoch, ...value }
    this.latest = packet
    for (const id of this.peers.keys()) this.sendLatest(id)
    await this.deliver(packet)
  }
  private async deliver(packet: Packet<T>) {
    if (this.controller.signal.aborted) return
    if (this.receiving) { this.queued = packet; return }
    this.receiving = true
    try {
      const epoch = `${packet.from}:${packet.epoch}`
      if (this.seenEpoch !== undefined && epoch !== this.seenEpoch) this.failure('实时数据流重新连接')
      this.seenEpoch = epoch
      if (packet.error !== undefined) this.failure(packet.error)
      else if (packet.value !== undefined) await this.receive(packet.value)
    } catch (error) { this.failure(error instanceof Error ? error.message : String(error)) }
    finally {
      this.receiving = false
      if (packet.from !== this.id) this.post({ type: 'ack', from: this.id, sequence: packet.sequence })
      const next = this.queued; this.queued = undefined
      if (next) void this.deliver(next)
    }
  }
  close() {
    this.post({ type: 'leave', from: this.id })
    this.controller.abort(); clearInterval(this.timer); this.channel?.close(); this.peers.clear(); this.queued = undefined
  }
}
