import { ref } from 'vue'
import type { TranslationProgress } from './ai-stream'
import type { Payload } from './commands'
import type { ToolCall } from './agent-capabilities'

export type InteractionPhase = 'idle' | 'waiting' | 'thinking' | 'streaming' | 'executing' | 'pending' | 'success' | 'error' | 'cancelled'
export type Outcome = 'running' | 'success' | 'error' | 'cancelled' | 'pending' | 'neutral'
export type AiRound = { id: number; reasoning: string; message: string; tool?: ToolCall; status?: string }
export type AiEntry = { id: number; input: string; reasoning: string; message: string; status: string; outcome: Outcome;
  operationId?: string; rounds: AiRound[]; finalAnswer?: string; facts: Payload[] }

// Presentation is deliberately separate from the executor's control/receipt state.
export function useCommandPresentation() {
  const phase = ref<InteractionPhase>('idle')
  const history = ref<AiEntry[]>([])
  let nextId = 0, activeId: number | undefined
  let successTimer: ReturnType<typeof setTimeout> | undefined
  const entry = (id: number | null | undefined = activeId) => history.value.find(value => value.id === id)
  function setPhase(value: InteractionPhase) {
    clearTimeout(successTimer)
    phase.value = value
  }
  function begin(input?: string) {
    activeId = input === undefined ? undefined : ++nextId
    if (input !== undefined) {
      history.value.push({ id: activeId!, input, reasoning: '', message: '', status: '', outcome: 'running', rounds: [], facts: [] })
      if (history.value.length > 50) history.value.splice(0, history.value.length - 50)
    }
    setPhase(input === undefined ? 'executing' : 'waiting')
  }
  function progress(value: TranslationProgress) {
    const current = entry()
    if (current) current[value.kind] = value.text
    setPhase(value.kind === 'reasoning' ? 'thinking' : 'streaming')
  }
  function beginRound(id: number) {
    const current = entry()
    if (!current) return
    current.rounds.push({ id, reasoning: '', message: '' })
    current.reasoning = current.message = ''
  }
  function roundProgress(id: number, value: TranslationProgress) {
    const round = entry()?.rounds.find(round => round.id === id)
    if (round) round[value.kind] = value.text
    progress(value)
  }
  function completeEntry(outcome: Outcome, status: string, id: number | null | undefined = activeId) {
    const current = entry(id)
    if (current) { current.outcome = outcome; current.status = status }
  }
  function finish(outcome: Outcome, status: string, id: number | null | undefined = activeId) {
    completeEntry(outcome, status, id)
    setPhase(outcome === 'neutral' ? 'idle' : outcome === 'running' ? 'executing' : outcome)
    if (outcome === 'success') successTimer = setTimeout(() => { phase.value = 'idle' }, 650)
  }
  return { phase, history, entry, begin, progress, beginRound, roundProgress, completeEntry, finish, setPhase, dispose: () => clearTimeout(successTimer) }
}
