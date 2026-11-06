// Accept only a complete single DSML invocation. Never extract a call from
// surrounding text or silently discard another invocation or parameter.
export function parseDsml(content: string): unknown {
  const call = /^\s*<｜DSML｜ calls>\s*<｜DSML｜ invoke name="([A-Za-z_][A-Za-z0-9_-]*)">([\s\S]*?)<\/｜DSML｜ invoke>\s*<\/｜DSML｜ calls>\s*$/.exec(content)
  if (!call) throw Error('AI 返回的工具选择不是有效 JSON 或受支持的 DSML，未执行。')

  const args: Record<string, unknown> = Object.create(null)
  let rest = call[2].trim()
  const parameter = /^<｜DSML｜ parameter name="([A-Za-z_][A-Za-z0-9_-]*)" string="(true|false)">([\s\S]*?)<\/｜DSML｜ parameter>/
  while (rest) {
    const match = parameter.exec(rest)
    if (!match || Object.hasOwn(args, match[1]) || match[3].includes('<｜DSML｜') || match[3].includes('</｜DSML｜')) {
      throw Error('AI 返回的 DSML 参数无效，未执行。')
    }
    try { args[match[1]] = match[2] === 'true' ? match[3] : JSON.parse(match[3]) }
    catch { throw Error('AI 返回的 DSML 参数无效，未执行。') }
    rest = rest.slice(match[0].length).trimStart()
  }

  let toolCall: unknown = { name: call[1], arguments: args }
  if (call[1] === 'tool_call') {
    if (Object.keys(args).length !== 2 || typeof args.name !== 'string' || !Object.hasOwn(args, 'arguments')) {
      throw Error('AI 返回的 DSML 工具选择无效，未执行。')
    }
    toolCall = { name: args.name, arguments: args.arguments }
  }
  return { message: '正在处理 AI 选择的工具。', tool_call: toolCall }
}
