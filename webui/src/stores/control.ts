import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { McpGateway } from '@/lib/mcp'
import { PlanExecutor, type CommandPlan, type Payload, type RcStatus } from '@/lib/commands'
import { readConfig, translate, type ApiConfig } from '@/lib/translator'

export type CameraStatus = {
  state: string; subscribers: number; source_sequence: number | null; frame_age_s: number | null;
  width: number; height: number; source_fps: number; encode_fps: number; output_fps: number; error: string | null;
}

export const useControlStore = defineStore('control', () => {
  const gateway = new McpGateway(new URL('/mcp', window.location.origin))
  const executor = new PlanExecutor(gateway)
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
  const visible = ref(!document.hidden)
  const apiReady = computed(() => config.value?.enabled === true && !!config.value.model)
  const directMode = computed(() => config.value?.llm === false)
  const inputReady = computed(() => !!config.value && (directMode.value || apiReady.value))
  const canReset = computed(() => connected.value && (!busy.value || translating.value) && !unresolved.value && !uncertain.value)
  let translation: AbortController | null = null
  let serial = 0
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
        executor.detach()
        executor.unresolvedOperation = null
        executor.uncertainSubmission = false
        executor.pendingRunId = null
        executor.pendingKind = null
        message.value = 'RC 已重启，上次操作结果不可恢复；旧清单已停止。'
        failed.value = true
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
        executor.pendingKind = pending.kind === 'tui_batch' ? 'tui_batch' : 'plan'
        persist()
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
    executor.detach()
    document.removeEventListener('visibilitychange', visibility)
    void gateway.close()
  }
  async function submit(text: string) {
    if (busy.value) return
    if (!config.value || !inputReady.value) { message.value = configError.value; failed.value = true; return }
    const token = ++serial
    busy.value = true
    translating.value = !directMode.value
    failed.value = false
    details.value = []
    message.value = directMode.value ? '正在提交命令批次…' : '正在理解指令…'
    translation = new AbortController()
    try {
      const original = await gateway.status()
      if (token !== serial || translation.signal.aborted) throw Error('已取消提交，未发送新指令。')
      if (directMode.value) {
        await executor.runText(text, original, emit)
        if (token === serial) message.value = '命令批次已执行'
        return
      }
      const list = await gateway.call('policies')
      const rows = Array.isArray(list.result) ? list.result as Payload[] : []
      const policies = rows.filter(row => row.valid === true && typeof row.id === 'string').map(row => row.id as string)
      const plan = await translate(config.value, text, {
        state: original.state, policy: original.policy, control_owner: original.control_owner,
      }, policies, translation.signal)
      if (token !== serial) return
      translating.value = false
      emit({ translation: plan.message, steps: plan.steps })
      await executor.run(plan, original, emit)
      message.value = plan.steps.length ? `${plan.message} · 已执行` : plan.message
    } catch (error) {
      if (token === serial) { message.value = error instanceof Error ? error.message : '执行失败'; failed.value = true }
    } finally {
      if (token === serial) { busy.value = translating.value = false; translation = null; persist() }
    }
  }
  async function reset() {
    if (!canReset.value) return
    translation?.abort()
    const token = ++serial
    translating.value = false
    busy.value = true
    failed.value = false
    details.value = []
    try {
      const plan: CommandPlan = { message: '复位', steps: [{ action: 'reset' }] }
      await executor.run(plan, await gateway.status(), emit)
      message.value = '复位指令已完成 · 保持暂停'
    } catch (error) { message.value = error instanceof Error ? error.message : '复位失败'; failed.value = true }
    finally { if (token === serial) busy.value = false; persist() }
  }
  function cancel() {
    translation?.abort()
    executor.cancel()
  }
  async function reconcile() {
    try {
      const receipt = await executor.reconcile()
      if (receipt) emit(receipt)
      message.value = executor.unresolvedOperation ? '操作仍在执行，稍后可再次查询回执。' : '操作回执已确认；旧清单不会继续。'
      failed.value = receipt?.status === 'failed' || receipt?.status === 'cancelled'
    } catch (error) { message.value = error instanceof Error ? error.message : '回执查询失败'; failed.value = true }
    finally { persist() }
  }
  function acknowledgeUnknown() {
    executor.acknowledgeUnknown()
    persist()
    message.value = '已解除本地锁定；旧清单不会重发。'
  }
  return { connected, status, cameras, configError, connectionError, apiReady, directMode, inputReady, busy, translating, message, failed,
    details, unresolved, uncertain, visible, canReset, start, stop, submit, reset, cancel, reconcile, acknowledgeUnknown }
})
