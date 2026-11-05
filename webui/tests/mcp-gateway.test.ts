import { expect, it, vi } from 'vitest'
import { McpGateway } from '../src/lib/mcp'
import { ToolRejectedError } from '../src/lib/commands'
function gateway(response: object) {
  const client = { callTool: vi.fn(async (_request: object, _options?: object) => response), close: vi.fn(async () => {}) }
  const g = new McpGateway(new URL('http://localhost/mcp'))
  vi.spyOn(g as any, 'connection').mockResolvedValue(client)
  return { g, client }
}
it('treats an unstructured write response as unknown acceptance, not proven rejection', async () => {
  const { g } = gateway({ content: [{ type: 'text', text: 'unexpected response' }] })
  const error = await g.call('take').catch(error => error)
  expect(error).toBeInstanceOf(Error)
  expect(error).not.toBeInstanceOf(ToolRejectedError)
})
it('distinguishes explicit admission rejection from an accepted failed receipt', async () => {
  const { g } = gateway({ isError: true, structuredContent: { error: 'already active', operation_id: 'other-episode' } })
  await expect(g.call('return_and_wait')).rejects.toBeInstanceOf(ToolRejectedError)
  const accepted = gateway({ isError: true, structuredContent: { operation_id: 'mine', status: 'failed', error: 'failed' } })
  await expect(accepted.g.call('take')).resolves.toMatchObject({ operation_id: 'mine', status: 'failed' })
})
it('uses the executor control guard and never sends a cancelled request', async () => {
  const { g, client } = gateway({ structuredContent: {} })
  const state = { run_id: 'current', control_epoch: 2, generation: 3, state: 'paused', control_owner: 'mcp', returning: false }
  await g.call('take', { expected_control: { run_id: 'forged' } }, state)
  expect(client.callTool.mock.calls[0][0]).toMatchObject({ arguments: { expected_control: { run_id: 'current', control_epoch: 2, generation: 3 } } })
  const controller = new AbortController(); controller.abort()
  await expect(g.call('take', {}, state, controller.signal)).rejects.toThrow()
  expect(client.callTool).toHaveBeenCalledTimes(1)
})
