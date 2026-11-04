import { expect, it, vi } from 'vitest'
const fake = vi.hoisted(() => ({ connect: vi.fn(), close: vi.fn(), listTools: vi.fn(async () => ({ tools: [{ name: 'command' }] })), callTool: vi.fn() }))
vi.mock('@modelcontextprotocol/client', () => ({
  Client: class { connect = fake.connect; close = fake.close; listTools = fake.listTools; callTool = fake.callTool },
  StreamableHTTPClientTransport: class {},
}))
import { McpGateway } from '../src/lib/mcp'

it('requires the new tool on older RC and never falls back to command or take', async () => {
  const gateway = new McpGateway(new URL('http://127.0.0.1/mcp'))
  await expect(gateway.call('tui_command', { text: 'reset' })).rejects.toThrow(/升级并重启 RC/)
  expect(fake.callTool).not.toHaveBeenCalled()
  await gateway.close()
})
