import { expect, it } from 'vitest'
import { PlanExecutor, type RcGateway, type Payload, type RcStatus } from '../src/lib/commands'

const status: RcStatus = { run_id: 'text-run', state: 'running', control_owner: 'policy', control_epoch: 2, generation: 4, returning: false }
function fixture() {
  const calls: [string, Payload][] = []
  let cancelled = false
  const gateway: RcGateway = {
    async status() { return status },
    async call(name, args = {}) {
      calls.push([name, args])
      if (name === 'cancel_tui_command') cancelled = true
      return { operation_id: 'batch-id', status: name === 'tui_command' ? 'queued' : cancelled ? 'cancelled' : 'succeeded',
        result: { kind: 'tui_batch', total: 2, completed: name === 'tui_command' ? 0 : 2, steps: [] } }
    },
  }
  return { gateway, calls }
}

it('sends the entire raw text once without take, translation or policy discovery', async () => {
  const { gateway, calls } = fixture()
  const executor = new PlanExecutor(gateway, { pollMs: 1 })
  const text = 'set example.params.message "a \\"quote\\""\nstatus'
  await executor.runText(text, status)
  expect(calls).toEqual([['tui_command', { text, wait: false }], ['get_operation', { operation_id: 'batch-id' }]])
  expect(executor.pendingKind).toBeNull()
})

it('cancels the server batch if stop arrives before its acceptance response', async () => {
  const { gateway, calls } = fixture()
  const original = gateway.call
  let release!: () => void
  const held = new Promise<void>(resolve => { release = resolve })
  gateway.call = async (name, args, expected) => {
    if (name === 'tui_command') await held
    return original(name, args, expected)
  }
  const executor = new PlanExecutor(gateway, { pollMs: 1 })
  const running = executor.runText('status\nstatus', status)
  await new Promise(resolve => setTimeout(resolve, 0))
  expect(executor.uncertainSubmission).toBe(true)
  executor.cancel()
  release()
  await expect(running).rejects.toThrow()
  expect(calls.filter(([name]) => name === 'cancel_tui_command')).toEqual([
    ['cancel_tui_command', { operation_id: 'batch-id' }],
  ])
  expect(executor.unresolvedOperation).toBeNull()
})

it('detaching only ends local waiting and preserves the ID for refresh recovery', async () => {
  const { gateway, calls } = fixture()
  const executor = new PlanExecutor(gateway, { pollMs: 1 })
  await expect(executor.runText('status', status, payload => {
    if (payload.operation_id) executor.detach()
  })).rejects.toThrow()
  expect(executor.unresolvedOperation).toBe('batch-id')
  expect(executor.pendingKind).toBe('tui_batch')
  expect(calls.map(([name]) => name)).toEqual(['tui_command'])
  await executor.reconcile()
  expect(executor.unresolvedOperation).toBeNull()
})

it('retains uncertain submission without retry when the response is lost', async () => {
  const { gateway, calls } = fixture()
  gateway.call = async (name, args = {}) => { calls.push([name, args]); throw Error('offline') }
  const executor = new PlanExecutor(gateway)
  await expect(executor.runText('status', status)).rejects.toThrow('offline')
  expect(executor.pendingKind).toBe('tui_batch')
  expect(executor.uncertainSubmission).toBe(true)
  expect(calls).toHaveLength(1)
})

it('sends stop immediately instead of waiting for the next receipt poll', async () => {
  const { gateway, calls } = fixture()
  const executor = new PlanExecutor(gateway, { pollMs: 50 })
  let accepted!: () => void
  const ready = new Promise<void>(resolve => { accepted = resolve })
  const running = executor.runText('status\nstatus', status, row => { if (row.operation_id) accepted() })
  await ready
  executor.cancel()
  await new Promise(resolve => setTimeout(resolve, 0))
  expect(calls.some(([name]) => name === 'cancel_tui_command')).toBe(true)
  await expect(running).rejects.toThrow()
})
