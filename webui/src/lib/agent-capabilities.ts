import Ajv2020 from 'ajv/dist/2020'
import type { ValidateFunction } from 'ajv'
import { isStatus, sameControl, type Payload, type RcGateway, type RcStatus } from './commands'

export type ToolDefinition = { name: string; description?: string; inputSchema: Payload; annotations?: Payload }
export type ToolCatalog = { instructions: string; tools: ToolDefinition[] }
export interface AgentGateway extends RcGateway { catalog(): Promise<ToolCatalog> }
export type PolicyCommand = { name: string; alias?: string | null; args: string[]; help: string; effect?: string | null; requires_take: boolean; timeout_s?: number }
export type PolicyCapability = { source_id: string; instance_id: string | null; loaded: boolean; runnable: boolean; commands: PolicyCommand[] }
export type CapabilitySnapshot = ToolCatalog & {
  status: RcStatus; policies: Payload; policy_commands: { policies: PolicyCapability[]; errors: unknown[] }; help: Payload; props: Payload;
  managed: Record<string, string[]>;
}
export type ToolCall = { name: string; arguments: Payload }
export type AgentDecision = { message: string; tool_call: ToolCall | null }
export type PreparedTool = { name: string; args: Payload; readOnly: boolean; episode: boolean; shutdown: boolean }
const excluded = new Set(['tui_command', 'cancel_tui_command'])
const managedFields = new Set(['expected_control', 'wait', 'timeout_s', 'include_images', 'cameras', 'image_quality'])
const ajv = new Ajv2020({ strict: false, strictNumbers: true, allErrors: true, coerceTypes: false, removeAdditional: false, addUsedSchema: false })
const validators = new Map<string, ValidateFunction>()
export const object = (value: unknown): value is Payload => !!value && typeof value === 'object' && !Array.isArray(value)
function validator(schema: Payload) {
  const key = JSON.stringify(schema)
  if (!validators.has(key)) {
    if (validators.size >= 128) validators.clear()
    validators.set(key, ajv.compile(schema))
  }
  return validators.get(key)!
}

export const decisionSchema = {
  type: 'object', required: ['message', 'tool_call'], additionalProperties: false,
  properties: { message: { type: 'string', minLength: 1, maxLength: 4000 }, tool_call: { anyOf: [
    { type: 'null' }, { type: 'object', required: ['name', 'arguments'], additionalProperties: false,
      properties: { name: { type: 'string', minLength: 1 }, arguments: { type: 'object' } } },
  ] } },
}
const checkDecision = validator(decisionSchema)

export async function discoverCapabilities(gateway: AgentGateway, expected?: RcStatus): Promise<CapabilitySnapshot> {
  const start = await gateway.status()
  if (!isStatus(start) || !start.run_id || (expected && !sameControl(start, expected))) throw Error('RC 控制代次已变化，已停止本次请求。')
  const [catalog, policies, policy_commands, help, props] = await Promise.all([
    gateway.catalog(), gateway.call('policies'), gateway.call('policy_commands'), gateway.call('help'), gateway.call('props'),
  ])
  const end = await gateway.status()
  if (!sameControl(start, end)) throw Error('获取能力目录期间 RC 状态变化，请重新提交。')
  if (!catalog.instructions || !Array.isArray(catalog.tools) || !catalog.tools.length
    || !Array.isArray(policies.result) || !Array.isArray(policy_commands.policies) || !Array.isArray(policy_commands.errors)
    || !Object.keys(help).length || !Object.keys(props).length) throw Error('MCP 能力目录不完整，请更新 RC。')
  for (const row of policy_commands.policies) {
    if (!object(row) || typeof row.source_id !== 'string' || (row.instance_id !== null && typeof row.instance_id !== 'string')
      || typeof row.loaded !== 'boolean' || typeof row.runnable !== 'boolean' || !Array.isArray(row.commands)) throw Error('策略能力目录格式错误。')
    for (const spec of row.commands) if (!object(spec) || typeof spec.name !== 'string' || typeof spec.help !== 'string'
      || !Array.isArray(spec.args) || !spec.args.every(type => ['str', 'int', 'float', 'bool'].includes(String(type)))
      || typeof spec.requires_take !== 'boolean' || typeof spec.timeout_s !== 'number' || !Number.isFinite(spec.timeout_s)
      || (spec.timeout_s <= 0 && spec.timeout_s !== -1)
      || (spec.effect !== null && typeof spec.effect !== 'string')) throw Error('策略命令描述不完整。')
  }
  const names = new Set<string>(), managed: Record<string, string[]> = Object.create(null)
  const tools = catalog.tools.filter(tool => !excluded.has(tool.name)).map(tool => {
    if (!tool.name || names.has(tool.name) || !tool.description?.trim() || !object(tool.inputSchema)
      || !object(tool.annotations) || typeof tool.annotations.readOnlyHint !== 'boolean') throw Error('MCP 工具描述、参数 Schema 或控制注解不完整，请更新 RC。')
    names.add(tool.name)
    const schema = structuredClone(tool.inputSchema)
    delete schema.$schema // Normalize transport schemas to the validator's supported dialect.
    const properties = object(schema.properties) ? schema.properties : {}
    managed[tool.name] = Object.keys(properties).filter(key => managedFields.has(key))
    for (const field of managed[tool.name]) delete properties[field]
    schema.properties = properties
    schema.required = Array.isArray(schema.required) ? schema.required.filter(field => !managedFields.has(String(field))) : []
    schema.additionalProperties = false
    validator(schema) // Fail discovery before sending an unusable catalog to the model.
    return { name: tool.name, description: tool.description, inputSchema: schema, annotations: tool.annotations }
  })
  if (!['status', 'policies', 'policy_commands', 'help', 'props'].every(name => names.has(name))) throw Error('MCP 能力目录缺少必要工具，请更新 RC。')
  return { instructions: catalog.instructions, tools, managed, status: end, policies,
    policy_commands: policy_commands as CapabilitySnapshot['policy_commands'], help, props }
}

export function validateDecision(value: unknown, snapshot: CapabilitySnapshot): AgentDecision {
  if (!checkDecision(value)) throw Error('AI 工具选择格式错误，未执行。')
  const decision = value as AgentDecision
  const call = decision.tool_call
  if (!call) return decision
  const definition = snapshot.tools.find(tool => tool.name === call.name)
  if (!definition || excluded.has(call.name)) throw Error('AI 选择了未发现或仅供人工使用的工具。')
  const check = validator(definition.inputSchema)
  if (!check(call.arguments)) throw Error(`工具 ${call.name} 参数不符合当前 Schema：${ajv.errorsText(check.errors)}`)
  if (call.name === 'policy_command') {
    const target = snapshot.policy_commands.policies.find(row => row.instance_id === call.arguments.instance_id)
    const spec = target?.commands.find(spec => spec.name === call.arguments.command)
    const args = call.arguments.args ?? []
    if (!target?.loaded || !target.runnable || !spec) throw Error('策略实例未运行、存在歧义或命令未注册，请重新查询能力。')
    if (!Array.isArray(args) || args.length !== spec.args.length || !args.every((arg, index) => {
      const type = spec.args[index]
      return type === 'str' ? typeof arg === 'string' : type === 'bool' ? typeof arg === 'boolean'
        : typeof arg === 'number' && Number.isFinite(arg) && (type !== 'int' || Number.isInteger(arg))
    })) throw Error('策略命令参数必须使用 manifest 声明的数量和 JSON 原生类型。')
  }
  if (call.name === 'command') {
    const line = String(call.arguments.line)
    const word = /^[A-Za-z][A-Za-z0-9_-]*(?=\s|$)/.exec(line.trim())?.[0]
    const policyWords = snapshot.policy_commands.policies.flatMap(row => row.commands.flatMap(spec => [spec.name, spec.alias]))
    if (!word || word === 'policy' || policyWords.includes(word)) throw Error('policy 注册命令请使用 policy_command 和原生类型参数；RC 命令名须直接书写。')
    if (['mcp', 'camera', 'monitor', 'manage-policy', 'snapshot'].includes(word) || word.startsWith('monitor-')) throw Error('本地面板和递归 MCP 命令不可由 AI 执行。')
    if (line.length > 4000 || /[\r\n]/.test(line)) throw Error('一次只能提交一条 RC 命令。')
  }
  if (call.name === 'retry_reload' && call.arguments.instance_id !== snapshot.status.retry_reload_instance) throw Error('请选择状态中明确列出的可重试实例。')
  return decision
}

export function prepareToolCall(call: ToolCall, snapshot: CapabilitySnapshot): PreparedTool {
  validateDecision({ message: '参数校验', tool_call: call }, snapshot)
  const definition = snapshot.tools.find(tool => tool.name === call.name)!
  const fields = snapshot.managed[call.name] || []
  const readOnly = call.name === 'record' ? (call.arguments.action ?? 'status') === 'status' : definition.annotations?.readOnlyHint === true
  if (!readOnly && !fields.includes('expected_control')) throw Error(`工具 ${call.name} 缺少控制代次保护，请同步升级 RC。`)
  const args = { ...call.arguments }
  if (fields.includes('wait')) args.wait = false
  if (fields.includes('timeout_s')) args.timeout_s = call.name === 'state' ? 3 : 0
  if (fields.includes('include_images')) args.include_images = false
  return { name: call.name, args, readOnly, episode: call.name === 'return_and_wait',
    shutdown: call.name === 'command' && ['exit', 'quit'].includes(String(args.line).trim()) }
}
