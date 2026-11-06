export type AuditContext = { requestId?: number; roundId?: number }
export type AuditSink = (type: string, data: unknown, context?: AuditContext) => void
export type AuditEvent = AuditContext & { sequence: number; timestamp: string; type: string; data: unknown }
// These IDs isolate local caches/logs; they are not authentication tokens.
export const sessionId = () => globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`

// Observers must never change command admission, cancellation or receipt semantics.
export function audit(sink: AuditSink | undefined, type: string, data: unknown, context?: AuditContext) {
  try { sink?.(type, data, context) } catch { /* Logging is observational. */ }
}

export class SessionLog {
  readonly startedAt = new Date().toISOString()
  readonly sessionId = sessionId()
  private events: AuditEvent[] = []
  private sequence = 0
  error = ''
  constructor(private changed: () => void = () => {}) {}
  get count() { return this.events.length }
  scope(context: AuditContext): AuditSink {
    const captured = { ...context }
    return (type, data, more) => {
      try {
        // Copy at the boundary: later changes to reactive/executor objects cannot rewrite history.
        const copy = JSON.parse(JSON.stringify(data ?? null))
        this.events.push({ ...captured, ...more, sequence: ++this.sequence, timestamp: new Date().toISOString(), type, data: copy })
      } catch { this.error = '部分 AI 日志记录失败，导出内容可能不完整。' }
      try { this.changed() } catch { /* UI errors must not escape the observer. */ }
    }
  }
  snapshot() {
    return { formatVersion: 1, sessionId: this.sessionId, startedAt: this.startedAt, exportedAt: new Date().toISOString(),
      complete: !this.error, recordingError: this.error || null,
      events: JSON.parse(JSON.stringify(this.events)) as AuditEvent[] }
  }
}
