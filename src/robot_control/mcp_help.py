"""MCP help uses registered schemas, never configuration values or UI code."""
import json

from .help_document import document


TOOL_HELP = {
    'status': ('查询与发现', '读取 RC 控制权、策略、连接与动作进度。'),
    'policies': ('查询与发现', '列出策略来源及静态配置校验结果，不加载模型。'),
    'show': ('查询与发现', '读取 RC、活动策略或全局共享状态。'),
    'help': ('查询与发现', '查看人类可读帮助及对应结构化帮助文档。'),
    'policy_commands': ('查询与发现', '发现策略参数、命令和效果；重复来源逐实例返回。'),
    'props': ('查询与发现', '读取设备配置、单位、方向和当前动作默认值。'),
    'state': ('查询与发现', '读取设备反馈，可等待新状态或附带指定相机图像。'),
    'events': ('查询与发现', '按游标读取 RC 日志事件。'),
    'get_operation': ('查询与发现', '读取或等待一个已接受操作的回执。'),
    'get_policy_result': ('查询与发现', '读取策略任务完成/终止结果，不消费结果。'),
    'take': ('控制与运动', '接管控制权，暂停新策略推理并保留模型。'),
    'return': ('控制与运动', '归还控制权，以新反馈重置上下文并恢复策略。'),
    'return_and_wait': ('控制与运动', '归还控制权并等待策略显式完成或终止。'),
    'send_command': ('控制与运动', '提交相对运动、夹爪、保持或 home 命令。'),
    'move_eef': ('控制与运动', '为指定机械臂提交绝对末端位置与姿态。'),
    'move_joints': ('控制与运动', '为指定机械臂提交六关节绝对角度。'),
    'connector_profiles': ('连接与策略', '只读列出命名连接配置，不建立连接。'),
    'switch_connector': ('连接与策略', '切换连接并按各实例的 on_connector_switch 适配，完成后暂停。'),
    'retry_connector': ('连接与策略', '完成必要清理后手动重试失败连接。'),
    'policy_chain': ('连接与策略', '按顺序应用策略来源列表，或只保存为默认链。'),
    'policy_config': ('连接与策略', '查询、设置或保存一个确切已加载实例的配置。'),
    'policy_command': ('连接与策略', '以严格 JSON 参数调用确切实例的声明命令。'),
    'retry_reload': ('连接与策略', '冷重试一个确切失败实例，保留其他健康实例。'),
    'record': ('记录与终端', '查询或切换录制，无需接管且不改变运动或策略等待任务。'),
    'command': ('记录与终端', '执行正式人类命令；Agent 写操作优先使用对应结构化工具。'),
    'tui_command': ('记录与终端', '仅供人工 WebUI 输入的 TUI 语义文本批次。Agent 应使用结构化工具。'),
    'cancel_tui_command': ('记录与终端', '人工取消文本批次尚未开始的行，已开始操作正常收尾。'),
}

PARAMETER_HELP = {
    'wait': '是否等待回执完成；false 仍返回已接受操作的 ID。',
    'timeout_s': '本次等待的最长秒数；超时不会取消已接受动作或任务。',
    'expected_control': '可选控制代次复核；值应取自最新 status，过期请求将被拒绝。',
    'run_id': '当前 RC 运行标识。', 'control_epoch': '当前控制权代次。', 'generation': '当前动作缓存代次。',
    'include_images': '是否附带相机 JPEG；终端只展示图像元数据。',
    'cameras': '所需相机名称列表，不能重复；名称见 props。',
    'image_quality': 'JPEG 质量；只影响返回的图像编码。',
    'clear_queue': '清除尚未开始的手动动作步骤，不中断当前步骤或已提交设备目标。',
    'commands': '字符串为一个步骤；外层列表按顺序串行；步骤内用 | 或内层字符串列表并行。',
    'operation_id': '已有操作或策略任务的 ID。',
    'instance_id': '确切已加载实例 ID；重复来源必须指定编号实例。',
    'policy_id': '策略来源或实例 ID；来源可匹配多个运行实例。',
    'command': '策略命令名；类型与执行影响见 policy_commands 或策略帮助。',
    'args': '按声明顺序提供严格 JSON 类型的参数；不进行字符串到数字的隐式转换。',
    'key': '策略自己解析的设置项名称。',
    'value_text': '原始配置文本，原样交给 policy.configure，不自动 JSON 解析。',
    'profile': 'connector_profiles 中的连接配置名称。',
    'policies': '按执行顺序排列的来源 ID，允许重复；每次出现产生独立实例。',
    'line': '一条正式人类命令，例如 help config rc set 或 policy list。',
    'text': '按换行分隔的人类命令，批次 FIFO 排队；非递归 MCP 入口。',
    'targets': '以 left/right 为键的目标对象，可只指定一侧；两侧在同一步并行。',
    'end_pos': '六个绝对值：[x,y,z,rx,ry,rz]；位置单位米，XYZ RPY 姿态单位弧度。',
    'joint_pos': '六个关节的绝对角度，单位弧度。',
    'gripper': '原始绝对夹爪命令值；未指定时保持当前值。',
    'speed': 'slow/medium/fast 速度档；具体速度和省略时的默认档查询 props。',
    'since_seq': '已知反馈序号；等待比此序号更新的状态。',
    'wait_for_new': '等待新反馈；等待本身不会使机器人运动。',
    'after_seq': '只返回序号大于该值的日志事件。',
    'limit': '此次最多返回的日志事件数量。',
    'scope': '读取的配置范围：rc、policy 或 global。',
    'tool': '要查看 JSON 参数详情的 MCP 工具名。',
    'topic': 'RC 完整命令路径，例如 config rc set。',
}


def _resolve(schema, root, seen=()):
    ref = schema.get('$ref')
    if not ref or not ref.startswith('#/') or ref in seen:
        return schema
    value = root
    for token in ref[2:].split('/'):
        value = value.get(token.replace('~1', '/').replace('~0', '~'), {})
    return {**_resolve(value, root, (*seen, ref)), **{k: v for k, v in schema.items() if k != '$ref'}}


def _variants(schema, root):
    resolved = _resolve(schema, root)
    variants = resolved.get('anyOf', resolved.get('oneOf'))
    if variants:
        return [_resolve(value, root) for value in variants]
    if 'allOf' in resolved:
        combined = {k: v for k, v in resolved.items() if k != 'allOf'}
        for part in resolved['allOf']:
            part = _resolve(part, root)
            combined = {**combined, **part, 'properties': {**combined.get('properties', {}), **part.get('properties', {})},
                        'required': list(dict.fromkeys([*combined.get('required', []), *part.get('required', [])]))}
        return [combined]
    return [resolved]


def _type(schema, root, depth=0):
    if depth > 12:
        return 'recursive'
    names = []
    for variant in _variants(schema, root):
        kind = variant.get('type', 'object' if 'properties' in variant or 'additionalProperties' in variant else 'enum' if 'enum' in variant else 'any')
        if kind == 'array':
            kind = 'array<' + _type(variant.get('items', {}), root, depth + 1) + '>'
        names.extend(kind if isinstance(kind, list) else [kind])
    return ' | '.join(dict.fromkeys(names))


def schema_parameters(schema):
    """Walk actual JSON schema, preserving absent/default-null and child requiredness."""
    rows, emitted = [], set()

    def walk(properties, required, prefix='', ancestors=(), depth=0):
        if depth > 12:
            return
        for name, raw in properties.items():
            if len(rows) >= 512:
                return
            path = prefix + name
            spec = _resolve(raw, schema)
            variants = _variants(spec, schema)
            if path not in emitted:
                row = {'name': path, 'type': _type(spec, schema), 'required': name in required,
                       'description': spec.get('description') or PARAMETER_HELP.get(name, '')}
                if prefix:
                    row['description'] = '父对象提供时适用。' + row['description']
                if 'default' in spec:
                    row['default'] = spec['default']
                enum = [item for part in variants for item in part.get('enum', [])]
                if enum:
                    row['choices'] = enum
                key_choices = spec.get('propertyNames', {}).get('enum')
                if key_choices:
                    row['description'] += ' 对象键候选：' + json.dumps(key_choices, ensure_ascii=False)
                constraints = [f'{key}={json.dumps(spec[key], ensure_ascii=False)}' for key in
                    ('minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'minLength', 'maxLength', 'minItems', 'maxItems', 'pattern') if key in spec]
                if constraints:
                    row['description'] += (' ' if row['description'] else '') + '约束：' + '，'.join(constraints)
                rows.append(row)
                emitted.add(path)
            reference = raw.get('$ref')
            if reference and reference in ancestors:
                continue
            lineage = (*ancestors, reference) if reference else ancestors
            for part in variants:
                walk(part.get('properties', {}), part.get('required', []), path + '.', lineage, depth + 1)
                child = part.get('items')
                if isinstance(child, dict):
                    for item in _variants(child, schema):
                        walk(item.get('properties', {}), item.get('required', []), path + '[].', lineage, depth + 1)
                child = part.get('additionalProperties')
                if isinstance(child, dict):
                    for item in _variants(child, schema):
                        walk(item.get('properties', {}), item.get('required', []), path + '.<key>.', lineage, depth + 1)
    root = _resolve(schema, schema)
    walk(root.get('properties', {}), root.get('required', []))
    return rows


SEND_SECTIONS = [
    {'title': '命令语法', 'body': 'left|right|both:motion[&motion][:value][@slow|medium|fast]\nleft/right 指单臂，both 同时作用于双臂。& 在同一子句组合动作，每种动作只能出现一次；数值必须有限，位移和角度必须为正，方向由动作名称决定。'},
    {'title': '动作与单位', 'body': 'forward/backward：基坐标系 +x/-x；left/right：+y/-y；up/down：+z/-z，单位米。\ntwist-clockwise/twist-counterclockwise、tilt-down-toward-table/tilt-up-away-from-table、turn-left-across-table/turn-right-across-table：工具局部坐标系旋转，单位弧度。省略步长、角度或速度时使用当前 direct_control 配置，先查询 props。'},
    {'title': '串行与并行', 'body': 'commands 字符串表示一个步骤，子句用 | 并行。外层列表是依次执行的串行步骤；其中的内层字符串列表表示同一步的并行子句。每个并行步骤中，同一机械臂最多出现一次；both 已占用两臂。'},
    {'title': '夹爪、保持与 home', 'body': 'open/close 可带原始绝对夹爪值；省略时使用 props 中的配置值，不可在同一子句同时 open 和 close。与位移组合时同一个 :value 同时作为距离/角度与夹爪绝对值，需按设备语义选择。\nhold 保持该臂，不能带数值或与其他动作组合；整个步骤不能全是 hold。home 将六个关节与夹爪置零，不使用 RC 的 reset_qpos；home 独立使用且不接受数值，可加 @speed。只有姿态运动或 home 支持 @speed。'},
    {'title': '控制权、等待与队列', 'body': '先 take 并确认 succeeded，再提交运动。clear_queue 只移除尚未开始的步骤；wait 等待回执，本次等待超时或断连不取消运动。queued/running 仅表示已接受或正在执行，用 get_operation 查询直到 succeeded/failed/cancelled。take、pause、switch 不取消已提交设备动作。'},
]


EXTRA = {
    'policy_config': 'show 无需 take；set/save 需 take。set 将 key/value_text 原样交给 configure，提示候选不会成为业务校验。候选仅可改 params/execution；生命周期回调在提交后执行。save 将确切实例配置写回来源目录；生命周期提交完成后保留 MCP 控制权并暂停。',
    'policy_command': '先 take，再使用确切实例调用。args 类型来自 manifest，help_details 只提供说明。handler 超时使用 manifest 的 timeout_s；本工具 timeout_s 仅限制本次等待。重载或初始化失败只能手动冷重试该实例。',
    'policy_chain': 'apply 等待整个旧组退出证明齐备后重建全组，完成后暂停，不自动保存也不复位。save 只保存所给来源顺序，不改变当前组或成员参数。两种写入都需要 take，完成后保留 MCP 控制权。',
    'retry_reload': '目标来自 status.retry_reload_instances。先确认该实例旧进程树、容器和外部读者退出，再冷启动；其他成员的在途图像必须独立释放，不因该实例退出而回收。需要 take，完成后暂停且保留 MCP 控制权。',
    'return_and_wait': '先 take，再调用此工具归还控制并等待策略报告。timeout_s 省略/null 表示不设等待超时，0 立即返回；超时、取消等待或断连只结束本次调用，任务继续。使用返回的 operation_id 调用 get_policy_result。completed 与 terminated 都是有效业务结果。',
    'take': '暂停新策略推理并使旧输出失效，保留模型；已提交设备动作不会因此取消。所有 MCP 客户端共享控制权，确认回执 succeeded 后再运动。',
    'return': '取消 RC 手动运动，重置策略上下文并恢复执行；成功后 MCP 回到只读。操作提交保留代次复核语义。',
    'record': 'status 只读；on/off/toggle 都无需 take，不改变运动、控制权或策略等待任务。off 成功表示接受的数据已刷盘，等待超时不取消停止操作。',
    'tui_command': '仅供人工文本入口，Agent 使用原结构化工具。无需 take，按 TUI 语义执行；仅文本批次 FIFO 排队。真实本地 TUI 的非空提交会清除未开始的批次行，已开始的生命周期操作正常收尾。禁止递归 mcp、本地面板和 snapshot。HTTP 断连不取消批次。',
    'cancel_tui_command': '只取消指定批次未开始的行；已经开始的行按原生命周期结束。重复取消幂等，查询 get_operation 等待父回执进入终态。',
    'move_eef': 'targets.left/right 使用绝对末端六维值，米与弧度；可选原始夹爪值与速度档。两侧同一步并行。需先 take，clear_queue 只清未开始步骤，等待超时不取消动作。',
    'move_joints': 'targets.left/right 使用六个绝对关节角，单位弧度；可选原始夹爪值与速度档。两侧同一步并行。需先 take，clear_queue 只清未开始步骤，等待超时不取消动作。',
    'switch_connector': '需要 take。切换连接后按每个实例 execution.on_connector_switch 执行 reset 或原进程重新初始化，保留 MCP 控制权并暂停。连接失败使用 retry_connector，策略适配失败使用 retry_reload；均不自动重试。',
    'retry_connector': '需要 take；先完成尚未确认的清理，再重试失败连接。完成后保留 MCP 控制权并暂停。',
    'command': '查询命令和录制无需 take，其他写入需要 take。help 返回可读文本与 help_document；普通 status/state/operation 保持结构化结果。本地面板、递归 mcp 命令不允许从 HTTP 调用。',
}

EXAMPLES = {
    'send_command': [('单臂平移，另一臂保持', '{"commands":"left:up:0.02@slow | right:hold","wait":true}'),
                     ('先双臂打开，再移动左臂', '{"commands":[["left:open","right:open"],"left:forward:0.01@slow"],"wait":true}')],
    'policy_config': [('设置消息', '{"instance_id":"example","action":"set","key":"params.message","value_text":"整理桌面"}')],
    'policy_command': [('调用声明命令', '{"instance_id":"example","command":"set-message","args":["整理桌面"]}')],
    'policy_chain': [('应用 example 自链', '{"policies":["example","example"],"action":"apply","wait":true}')],
    'retry_reload': [('重试确切实例', '{"instance_id":"example-1","wait":true}')],
    'move_eef': [('绝对末端目标', '{"targets":{"left":{"end_pos":[0.3,0.1,0.2,0,0,0],"speed":"slow"}},"wait":true}')],
    'move_joints': [('六关节目标', '{"targets":{"left":{"joint_pos":[0,0,0,0,0,0],"speed":"slow"}},"wait":true}')],
    'command': [('查询 RC 配置命令帮助', '{"line":"help config rc set"}')],
    'help': [('查询策略命令', '{"policy_id":"example","command":"set-message"}')],
    'switch_connector': [('切换命名连接', '{"profile":"mock","wait":true}')],
    'record': [('开始录制', '{"action":"on","wait":true}')],
    'show': [('读取 RC 配置', '{"scope":"rc"}')],
    'get_operation': [('等待已有回执', '{"operation_id":"<操作ID>","wait":true}')],
    'tui_command': [('人工文本批次', '{"text":"pause\\nstatus","wait":true}')],
    'cancel_tui_command': [('取消人工批次', '{"operation_id":"<批次ID>"}')],
}


def mcp_help_document(tools, tool_name=None):
    selected = [tool for tool in tools if tool_name is None or tool['name'] == tool_name]
    if tool_name is not None and not selected:
        raise ValueError('未知 MCP 工具：' + tool_name)
    if tool_name is None:
        return document('MCP 工具帮助', 'mcp help <工具名> 查看 JSON 参数、执行影响与示例。',
            usage=['mcp <工具> [JSON对象]'],
            sections=[{'title': '控制权与等待', 'body': '默认只读；先 mcp take，确认 succeeded 后发动作。录制是无需接管的明确例外。wait=true 在后台等待，超时不取消动作；终端只显示图像元数据。'}],
            commands=[{'group': TOOL_HELP.get(tool['name'], ('其他工具', ''))[0], 'command': 'mcp ' + tool['name'] + ' [JSON对象]',
                'summary': TOOL_HELP.get(tool['name'], ('', tool.get('description', '')))[1]} for tool in selected])
    tool = selected[0]
    _, summary = TOOL_HELP.get(tool_name, ('', tool.get('description', '')))
    schema = tool.get('input_schema', {})
    sections = list(SEND_SECTIONS) if tool_name == 'send_command' else []
    if tool_name in EXTRA:
        sections.append({'title': '执行影响', 'body': EXTRA[tool_name]})
    if not sections and tool.get('description'):
        sections.append({'title': '工具说明', 'body': tool['description']})
    examples = EXAMPLES.get(tool_name)
    if examples is None and not schema.get('required'):
        examples = [('调用示例', '{}')]
    return document('MCP ' + tool_name, summary, usage=['mcp ' + tool_name + ' [JSON对象]'],
        sections=sections, parameters=schema_parameters(schema),
        examples=[{'title': title, 'command': 'mcp ' + tool_name + ' ' + args} for title, args in examples or []])
