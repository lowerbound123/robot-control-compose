import { afterEach, expect, it, vi } from 'vitest'
import { SharedFeed } from '../src/lib/shared-feed'

afterEach(() => vi.unstubAllGlobals())

it.each([{}, undefined])('receives and stops a direct feed without randomUUID (crypto=%s)', async crypto => {
  vi.stubGlobal('crypto', crypto)
  vi.stubGlobal('navigator', {})
  vi.stubGlobal('BroadcastChannel', undefined)
  const receive = vi.fn(), fail = vi.fn()
  let signal!: AbortSignal
  let publish!: (value: string) => Promise<void>
  const feed = new SharedFeed<string>('test-camera', async (current, send) => {
    signal = current
    publish = send
    await send('first frame')
  }, receive, fail)
  try {
    await vi.waitFor(() => expect(receive).toHaveBeenCalledWith('first frame'))
    expect(fail).not.toHaveBeenCalled()
    feed.close()
    expect(signal.aborted).toBe(true)
    await publish('late frame')
    expect(receive).toHaveBeenCalledTimes(1)
  } finally { feed.close() }
})
