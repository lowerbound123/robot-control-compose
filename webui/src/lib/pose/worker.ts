import { PoseHistory, reducePoints } from './history'
import { SharedFeed } from '../shared-feed'
import type { PoseWorkerInput, PoseWorkerOutput } from './types'

const history = new PoseHistory()
const clock = () => performance.timeOrigin + performance.now()
const send = (message: PoseWorkerOutput) => postMessage(message)
let connected = false, lastReport = 0
let feed: SharedFeed<string> | undefined

function report(force = false) {
  if (!force && clock() - lastReport < 200) return
  lastReport = clock()
  send({ type: 'status', status: history.status(connected) })
}
function disconnect(error: string) { connected = false; history.disconnect(error); report(true) }
function stream(url: string, signal: AbortSignal, publish: (value: string) => Promise<void>, fail: (error: string) => void) {
  return new Promise<void>(resolve => {
    let source: EventSource | undefined, received = clock()
    const connect = () => {
      source?.close()
      if (signal.aborted) return
      received = clock()
      source = new EventSource(url)
      source.onmessage = event => { received = clock(); void publish(event.data) }
      source.onerror = () => fail('状态流已断开，正在重连；请确认 RC 已支持状态流')
    }
    const timer = setInterval(() => {
      if (clock() - received > 3000) { fail('状态流超时，正在重连'); connect() }
    }, 1000)
    signal.addEventListener('abort', () => { source?.close(); clearInterval(timer); resolve() }, { once: true })
    connect()
  })
}

onmessage = (event: MessageEvent<PoseWorkerInput>) => {
  const message = event.data
  if (message.type === 'start') {
    feed?.close()
    feed = new SharedFeed(message.url, (signal, publish, fail) => stream(message.url, signal, publish, fail), data => {
      try {
        history.receive(JSON.parse(data), clock())
        connected = true; report()
      } catch (error) {
        disconnect(error instanceof Error ? error.message : '状态流处理失败')
        // Never silently truncate previous history on allocation failure.
        if (error instanceof RangeError) { feed?.close(); feed = undefined }
      }
    }, disconnect)
  } else if (message.type === 'stop') {
    feed?.close(); feed = undefined; disconnect('状态流已断开，正在重连')
  } else {
    send({ type: 'view', id: message.id, status: history.status(connected), charts: message.charts.map(chart => ({
      ...chart, points: reducePoints(history.slice(chart.mode, chart.channel, chart.start, chart.end), chart.start, chart.end, chart.pixels),
    })) })
  }
}
