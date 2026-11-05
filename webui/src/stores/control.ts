import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { McpGateway } from '@/lib/mcp'
import { PlanExecutor, type Payload, type RcStatus } from '@/lib/commands'
import { readConfig, type ApiConfig } from '@/lib/translator'
import { useCommandPresentation } from '@/lib/command-presentation'
import { runAgent } from '@/lib/agent'

export type CameraStatus = {
  state: string; subscribers: number; source_sequence: number | null; frame_age_s: number | null;
  width: number; height: number; source_fps: number; encode_fps: number; output_fps: number; error: string | null;
}

export const useControlStore = defineStore('control', () => {
  const gateway = new McpGateway(new URL('/mcp', window.location.origin))
  const executor = new PlanExecutor(gateway)
  const presentation = useCommandPresentation()
  const { phase, history } = presentation
  const connected = ref(false)
  const status = ref<RcStatus | null>(null)
  const cameras = ref<Record<string, CameraStatus>>({})
  const config = ref<ApiConfig | null>(null)
  const configError = ref('')
  const connectionError = ref('')
  const busy = ref(false)
  const translating = ref(false)
  const message = ref('')
  const failed = ref(false)
  const details = ref<Payload[]>([])
  const unresolved = ref<string | null>(null)
  const uncertain = ref(false)
  const showRecovery = computed(() => !busy.value && (!!unresolved.value || uncertain.value))
  const visible = ref(!document.hidden)
  const apiReady = computed(() => config.value?.enabled === true && !!config.value.model)
  const directMode = computed(() => config.value?.llm === false)
  const inputReady = computed(() => !!config.value && (directMode.value || apiReady.value))
  const canReset = computed(() => connected.value && (!busy.value || translating.value) && !unresolved.value && !uncertain.value)
  const policyRunning = computed(() => status.value?.state === 'running')
  const canTogglePause = computed(() => canReset.value && !status.value?.returning && ['running', 'paused'].includes(status.value?.state || ''))
  let translation: AbortController | null = null
  let serial = 0
  let cancelledSerial = -1
  let uncertainHistoryId: number | undefined
  let stopped = true
  let timer: ReturnType<typeof setTimeout> | undefined
  let pollController: AbortController | undefined

  function persist() {
    unresolved.value = executor.unresolvedOperation
    uncertain.value = executor.uncertainSubmission
    try {
      if (unresolved.value || uncertain.value) sessionStorage.setItem('rc-pending-operation', JSON.stringify({
        id: unresolved.value, uncertain: uncertain.value, run: executor.pendingRunId, kind: executor.pendingKind,
      }))
      else sessionStorage.removeItem('rc-pending-operation')
    } catch { /* Browser storage can be unavailable; live state is retained. */ }
  }
  function emit(value: Payload) {
    const batch = value.result as Payload | undefined
    details.value = batch?.kind === 'tui_batch' ? [value] : [...details.value, value].slice(-40)
    if (batch?.kind === 'tui_batch') message.value = `${batch.completed}/${batch.total} · 命令批次${value.status === 'queued' ? '排队中' : '执行中'}`
    else if (value.operation_id && executor.pendingKind === 'tui_batch') message.value = '命令批次已排队'
    if (typeof value.label === 'string') message.value = `${value.step}/${value.total} · ${value.label}`
    if (value.operation_id && !['succeeded', 'failed', 'cancelled'].includes(String(value.status)) && executor.pendingKind !== 'tui_batch') {
      message.value = '正在执行，等待操作回执…'
    }
    const current = presentation.entry()
    if (current) {
      current.status = message.value
      if (typeof value.operation_id === 'string') current.operationId = value.operation_id
      const round = current.rounds.at(-1)
      if (round && typeof value.status === 'string') round.status = value.status
    }
    persist()
  }
  async function poll() {
    if (stopped) return
    pollController = new AbortController()
    const timeout = setTimeout(() => pollController?.abort(), 5000)
    try {
      const [health, preview] = await Promise.all([
        fetch('/health', { cache: 'no-store', signal: pollController.signal }),
        fetch('/preview/status', { cache: 'no-store', signal: pollController.signal }),
      ])
      if (!health.ok || !preview.ok) throw Error(`RC 连接异常（HTTP ${!health.ok ? health.status : preview.status}）`)
      const next: RcStatus = await health.json()
      const previewBody = await preview.json()
      if (stopped) return
      if (executor.pendingRunId && next.run_id !== executor.pendingRunId && (unresolved.value || uncertain.value)) {
        const recordId = history.value.find(value => value.operationId === executor.unresolvedOperation)?.id ?? uncertainHistoryId ?? null
        executor.detach()
        executor.unresolvedOperation = null
        executor.uncertainSubmission = false
        executor.pendingRunId = null
        executor.pendingKind = null
        message.value = 'RC 已重启，上次操作结果不可恢复；旧清单已停止。'
        failed.value = true
        presentation.finish('error', message.value, recordId)
        uncertainHistoryId = undefined
      }
      status.value = next
      cameras.value = previewBody.cameras
      connected.value = true
      connectionError.value = ''
      persist()
    } catch (error) {
      if (!stopped) {
        connected.value = false
        connectionError.value = error instanceof Error ? error.message : 'RC 连接失败'
      }
    } finally {
      clearTimeout(timeout)
      if (!stopped) timer = setTimeout(poll, 1000)
    }
  }
  function visibility() { visible.value = !document.hidden }
  async function start() {
    stopped = false
    document.addEventListener('visibilitychange', visibility)
    try {
      const pending = JSON.parse(sessionStorage.getItem('rc-pending-operation') || 'null')
      if (pending && (typeof pending.id === 'string' || pending.uncertain === true)) {
        executor.unresolvedOperation = typeof pending.id === 'string' ? pending.id : null
        executor.uncertainSubmission = pending.uncertain === true
        executor.pendingRunId = typeof pending.run === 'string' ? pending.run : null
        executor.pendingKind = pending.kind === 'tui_batch' ? 'tui_batch' : pending.kind === 'policy_episode' ? 'policy_episode' : 'plan'
        persist()
        presentation.setPhase('pending')
      }
    } catch { /* Ignore invalid local UI recovery data. */ }
    void poll()
    try {
      const response = await fetch('/config.json', { cache: 'no-store' })
      if (!response.ok) throw Error('无法读取 WebUI 公共配置，请检查服务端配置并重启 WebUI 服务。')
      config.value = readConfig(await response.json())
      if (!inputReady.value) configError.value = 'AI 未配置 · 请配置服务端接口并重启 WebUI 服务'
    } catch (error) { configError.value = error instanceof Error ? error.message : 'API 配置读取失败' }
  }
  function stop() {
    stopped = true
    clearTimeout(timer)
    pollController?.abort()
    translation?.abort()
    serial++
    presentation.dispose()
    executor.detach()
    document.removeEventListener('visibilitychange', visibility)
    void gateway.close()
  }
  async function submit(text: string) {
    if (busy.value) return
    if (!config.value || !inputReady.value) { message.value = configError.value; failed.value = true; return }
    const token = ++serial
    const previousPendingId = executor.unresolvedOperation
    const previouslyUncertain = executor.uncertainSubmission
    presentation.begin(directMode.value ? undefined : text.trim())
    busy.value = true
    translating.value = !directMode.value
    failed.value = false
    details.value = []
    message.value = directMode.value ? '正在提交命令批次…' : '正在理解指令…'
    const request = new AbortController()
    translation = request
    const emitCurrent = (value: Payload) => { if (token === serial) emit(value) }
    try {
      const original = await gateway.status()
      if (token !== serial || request.signal.aborted) throw Error('已取消提交，未发送新指令。')
      if (directMode.value) {
        await executor.runText(text, original, emitCurrent)
        if (token === serial) { message.value = '命令批次已执行'; presentation.finish('success', message.value) }
        return
      }
      const active = () => token === serial && !request.signal.aborted
      const result = await runAgent({ config: config.value, input: text, gateway, executor, signal: request.signal,
        history: history.value.map(entry => ({ input: entry.input, message: entry.finalAnswer ?? entry.status,
          status: entry.status, outcome: entry.outcome, facts: entry.facts })),
        onRound: round => { if (active()) presentation.beginRound(round) },
        onPhase: phase => {
          if (!active()) return
          translating.value = phase === 'generating'
          presentation.setPhase(phase === 'generating' ? 'waiting' : 'executing')
          message.value = phase === 'generating' ? '正在理解指令与工具结果…' : '正在执行工具，等待操作回执…'
        },
        onProgress: (round, value) => {
          if (!active()) return
          presentation.roundProgress(round, value)
          message.value = value.kind === 'reasoning' ? '思考中…' : '正在生成操作说明…'
        },
        onTool: (roundId, call, payload) => {
          if (token !== serial) return
          const current = presentation.entry()
          const round = current?.rounds.find(round => round.id === roundId)
          if (round) { round.tool = call; round.status = payload ? String(payload.status ?? 'succeeded') : 'running' }
          if (payload && current) current.facts.push({ tool: call.name, arguments: call.arguments,
            operation_id: payload.operation_id ?? null, status: payload.status ?? 'succeeded', run_id: original.run_id })
          emit({ tool: call.name, arguments: call.arguments, ...(payload ? { result: payload } : {}) })
        },
        emit: emitCurrent,
      })
      if (token !== serial) return
      if (request.signal.aborted) throw Error('已停止后续工具调用。')
      translating.value = false
      const current = presentation.entry()
      if (current) { current.message = current.finalAnswer = result.message; current.facts = result.facts }
      message.value = result.message
      presentation.finish(result.outcome === 'terminated' ? 'neutral' : result.outcome, message.value)
    } catch (error) {
      if (token === serial) {
        message.value = error instanceof Error ? error.message : '执行失败'
        failed.value = token !== cancelledSerial
        presentation.finish(token === cancelledSerial ? 'cancelled' : 'error', message.value)
      }
    } finally {
      if (token === serial) {
        busy.value = translating.value = false; translation = null; persist()
        if (unresolved.value || uncertain.value) {
          const current = presentation.entry()
          if ((unresolved.value && (unresolved.value !== previousPendingId || current?.operationId === unresolved.value))
            || (uncertain.value && !previouslyUncertain)) presentation.finish('pending', message.value)
          else presentation.setPhase('pending')
          if (uncertain.value && !previouslyUncertain) uncertainHistoryId = current?.id
        }
      }
    }
  }
  async function runDirect(action: 'reset' | 'pause' | 'resume') {
    if (action === 'reset' ? !canReset.value : !canTogglePause.value) return
    const label = { reset: '复位', pause: '暂停', resume: '恢复' }[action]
    translation?.abort()
    if (translating.value) presentation.finish('cancelled', `已停止翻译，改为${label}。`)
    const token = ++serial
    presentation.begin()
    translating.value = false
    busy.value = true
    failed.value = false
    details.value = []
    message.value = `正在${label}…`
    try {
      const original = await gateway.status()
      if (token !== serial || token === cancelledSerial) throw Error('已停止提交，未发送新指令。')
      // An explicit toolbar click is human input, like direct command mode.
      // Keep the shared receipt/guard handling, without an implicit MCP take.
      await executor.runText(action, original, value => { if (token === serial) emit(value) })
      if (token !== serial) return
      message.value = action === 'reset' ? '复位指令已完成 · 保持暂停' : action === 'pause' ? '已暂停策略运行' : '已恢复策略运行'
      presentation.finish('success', message.value)
    } catch (error) {
      if (token === serial) {
        message.value = error instanceof Error ? error.message : `${label}失败`; failed.value = token !== cancelledSerial
        presentation.finish(token === cancelledSerial ? 'cancelled' : 'error', message.value)
      }
    } finally {
      if (token === serial) {
        busy.value = false; persist()
        if (unresolved.value || uncertain.value) presentation.finish('pending', message.value)
      }
    }
  }
  function reset() { return runDirect('reset') }
  function togglePause() { return runDirect(policyRunning.value ? 'pause' : 'resume') }
  function cancel() {
    cancelledSerial = serial
    translation?.abort()
    executor.cancel()
  }
  async function reconcile() {
    if (busy.value) return
    const token = serial
    const id = history.value.find(value => value.operationId === executor.unresolvedOperation)?.id ?? null
    try {
      const receipt = await executor.reconcile()
      const terminated = receipt?.status === 'succeeded' && (receipt.result as Payload | undefined)?.outcome === 'terminated'
      const outcome = executor.unresolvedOperation ? 'pending' : terminated ? 'neutral' : receipt?.status === 'succeeded' ? 'success'
        : receipt?.status === 'failed' ? 'error' : receipt?.status === 'cancelled' ? 'cancelled' : 'neutral'
      const summary = outcome === 'pending' ? '操作仍在执行，稍后可再次查询回执。'
        : terminated ? '策略任务已终止，未报告完成；旧清单不会继续。'
        : outcome === 'error' ? '原操作执行失败；旧清单不会继续。'
        : outcome === 'cancelled' ? '原操作已取消；旧清单不会继续。' : '操作回执已确认；旧清单不会继续。'
      if (token !== serial) { presentation.completeEntry(outcome, summary, id); return }
      if (receipt) details.value = [...details.value, receipt].slice(-40)
      message.value = summary
      failed.value = receipt?.status === 'failed' || receipt?.status === 'cancelled'
      presentation.finish(outcome, message.value, id)
    } catch (error) {
      if (token !== serial) return
      message.value = error instanceof Error ? error.message : '回执查询失败'; failed.value = true
      presentation.setPhase('pending')
    }
    finally { persist() }
  }
  function acknowledgeUnknown() {
    executor.acknowledgeUnknown()
    persist()
    message.value = '已解除本地锁定；旧清单不会重发。'
    presentation.finish('neutral', message.value, uncertainHistoryId ?? null)
    uncertainHistoryId = undefined
    if (unresolved.value) presentation.setPhase('pending')
  }
  return { connected, status, cameras, configError, connectionError, apiReady, directMode, inputReady, busy, translating, message, failed,
    details, unresolved, uncertain, visible, canReset, canTogglePause, policyRunning, phase, history, showRecovery,
    start, stop, submit, reset, togglePause, cancel, reconcile, acknowledgeUnknown }
})
