"""Chinese operator help, built from the shared command catalog."""
import re

from rc_policy_sdk.command_syntax import COMMAND_TREE
from .help_document import document


# Each description explains observable behavior; arguments come from the grammar.
BUILTINS = {
    'help': ('控制与状态', '按功能查看命令，或查看一个完整命令路径的详情。', '查询不会连接设备、加载策略或改变控制权。策略详情使用 policy help；MCP 工具详情使用 mcp help。', 'help config rc set'),
    'status': ('控制与状态', '查看控制权、暂停状态、活动策略、连接和进行中的操作。', '只读；操作处于 queued/running 时尚未完成，不能据此当作 succeeded。', 'status'),
    'pause': ('控制与状态', '暂停新的策略推理和动作提交。', '保留已加载模型与上下文；已提交给设备的目标不会取消。', 'pause'),
    'resume': ('控制与状态', '恢复策略调度。', '故障或生命周期操作未完成时不能靠 resume 修复；需要按状态中的失败实例手动重试。', 'resume'),
    'reset': ('控制与状态', '按 RC 的 reset_qpos 执行复位并重置策略上下文。', '这是运动与生命周期操作。MCP 调用前需要 take，完成后保留 MCP 控制权并暂停策略。', 'reset'),
    'exit': ('控制与状态', '按统一清理期限退出 RC。', '等待策略进程、容器和图像读者清理；强制清理会返回非零退出码。', 'exit'),
    'connector list': ('机器人连接', '列出可用机器人连接配置及其校验状态。', '只读取配置，不建立连接、不切换活动设备。', 'connector list'),
    'connector status': ('机器人连接', '查看当前连接、代次和待重试实例。', '只读；连接失败与策略适配失败分别展示。', 'connector status'),
    'connector switch': ('机器人连接', '切换到指定连接配置，并按各实例的 on_connector_switch 适配上下文。', '切换后保持暂停。MCP 需先 take；不会自动重试，已提交设备动作不由此取消。', 'connector switch mock'),
    'connector retry': ('机器人连接', '显式重试失败的机器人连接。', '先完成尚未证明结束的清理，再重试连接。MCP 需先 take；策略实例失败使用 policy <实例> retry。', 'connector retry'),
    'config rc show': ('策略与配置', '查看 RC 当前配置、待保存草稿和差异。', '只读；公开输出会隐藏 api_key。', 'config rc show'),
    'config rc set': ('策略与配置', '修改 RC 配置草稿中的字段。', '仅编辑草稿；config rc save 落盘，重启 RC 后生效。不会热改当前控制周期。密钥只能在配置文件中修改。', 'config rc set frequency_hz 20'),
    'config rc save': ('策略与配置', '把 RC 配置草稿保存到当前配置文件。', '保存不改变当前运行配置，重启 RC 后生效。MCP 写入需先 take。', 'config rc save'),
    'global show': ('共享数据', '读取全局共享 JSON 状态及版本，可限定一个键。', '只读；全局数据由所有已加载实例共享。', 'global show example_note'),
    'global set': ('共享数据', '设置一个共享键的 JSON 值。', '提交到 RC 共享状态服务，后续策略读取会看到新版本；不会触发策略重载。', 'global set example_note "整理桌面"'),
    'global remove': ('共享数据', '删除一个全局共享键。', '修改共享状态并更新版本；MCP 写入需先 take。', 'global remove example_note'),
    'pose save': ('运动与记录', '保存当前机器人完整状态，供后续恢复。', '写入 outputs/state_saves；名称是状态文件名称，不是策略实例。', 'pose save before-task'),
    'pose restore': ('运动与记录', '从已保存名称或路径恢复机器人姿态。', '会产生机器人运动。MCP 需先 take；先核实状态和操作回执再继续。', 'pose restore before-task'),
    'snapshot': ('运动与记录', '保存三路原始相机 PNG 与完整 state 快照。', '在后台写入当前工作目录 snapshot/<时间戳>/，支持本地 TUI/headless；HTTP 文本入口不可用。', 'snapshot'),
    'record': ('运动与记录', '切换录制开关。', '无需 take，不改变运动、控制权或策略等待任务。停止会等待已接受数据写盘。', 'record'),
    'record start': ('运动与记录', '开始录制相机、状态和控制记录。', '无需 take，不重置策略，也不改变当前动作。', 'record start'),
    'record stop': ('运动与记录', '停止录制并等待已接受数据写盘。', '成功回执表示录制数据已刷盘；等待超时不取消写盘。', 'record stop'),
    'record status': ('运动与记录', '查看录制会话、状态和错误。', '只读；录制失败不自动接管策略控制权。', 'record status'),
    'camera': ('本地面板', '切换 TUI 相机面板的显示状态。', '省略参数等价 camera toggle。仅支持本地 TUI；不改变机器人运动。', 'camera'),
    'camera show': ('本地面板', '显示 TUI 相机面板。', '仅支持本地 TUI；重复执行保持显示。', 'camera show'),
    'camera hide': ('本地面板', '隐藏 TUI 相机面板。', '仅支持本地 TUI；重复执行保持隐藏。', 'camera hide'),
    'camera toggle': ('本地面板', '在显示和隐藏之间切换相机面板。', '仅支持本地 TUI，与裸 camera 相同。', 'camera toggle'),
    'monitor': ('本地面板', '切换机器人或某个已加载策略实例的监控面板。', '仅支持本地 TUI；不带实例时切换机器人面板，指定实例时切换该实例面板。启动时关闭。', 'monitor example'),
    'policy help': ('策略与配置', '读取所有策略、一个来源或一个实例的静态帮助。', '不导入模型、不启动容器；重复来源逐实例展示。可追加自定义命令名缩小范围。', 'policy help example set-message'),
    'policy list': ('策略与配置', '列出策略来源及其可用性。', '只读静态 manifest，不加载模型；运行实例和来源目录分别标识。', 'policy list'),
    'policy switch': ('策略与配置', '切换策略来源或原生链，重建整个执行组。', '等待整个旧组的进程、容器和外部读者退出证明后再启动新组。--reset 额外复位；MCP 需 take 且完成后暂停。', 'policy switch example'),
    'policy manage': ('本地面板', '打开本地 TUI 链编排面板。', '进入时暂停；每次重复来源都是独立实例，按出现顺序编号。', 'policy manage'),
    'policy manage apply': ('本地面板', '应用链编排草稿并重建执行组。', '应用后暂停；不自动保存默认链。仅支持本地 TUI。', 'policy manage apply'),
    'policy manage save': ('本地面板', '保存链编排草稿为默认链。', '只保存草稿，不应用运行组，也不保存实例参数。仅支持本地 TUI。', 'policy manage save'),
    'policy manage close': ('本地面板', '关闭链编排面板。', '关闭面板不会自动恢复策略运行。仅支持本地 TUI。', 'policy manage close'),
    'policy <实例> config show': ('策略与配置', '读取一个确切已加载实例的已提交配置。', '只读；重复来源必须指定唯一实例，不按来源合并配置。', 'policy example config show'),
    'policy <实例> config set': ('策略与配置', '把设置项与原始文本完整交给该实例的 configure 解析。', 'RC 不按提示候选做业务校验。策略只能修改 params/execution，并可在提交后请求 reset 或原进程 reinitialize；失败需手动冷重试。', 'policy example config set params.message 整理桌面'),
    'policy <实例> config save': ('策略与配置', '把一个确切实例的已提交配置写回来源目录。', '只保存指定实例；重复来源共享同一来源文件，保存者必须明确。MCP 写入需 take。', 'policy example config save'),
    'policy <实例> retry': ('策略与配置', '手动冷重试状态中失败的确切策略实例。', '先证明该实例的进程树、容器和外部读者已退出；保留其他成员及其独立图像租约。MCP 需 take，完成后仍暂停。', 'policy example retry'),
    'policy <实例> <命令>': ('策略与配置', '调用该实例 manifest 声明的自定义命令。', '参数、handler 超时及 effect 由策略声明。使用 policy help <实例> <命令> 查看详情。MCP 调用需 take。', 'policy example set-message "整理桌面"'),
    'mcp help': ('MCP 控制', '查看 MCP 工具目录或一个工具的 JSON 参数详情。', '只读；字段类型、必填和默认值来自实际工具 schema。', 'mcp help send_command'),
    'mcp take': ('MCP 控制', '通过结构化工具接管控制权并暂停策略。', '先确认 succeeded，再提交动作。保留已加载模型；不会取消已经提交给设备的动作。', 'mcp take {"wait":true}'),
    'mcp return': ('MCP 控制', '归还控制权并以新反馈重置策略上下文后恢复执行。', '取消 RC 中的手动运动队列；MCP 回到只读。', 'mcp return {"wait":true}'),
    'mcp <工具>': ('MCP 控制', '在终端后台调用 MCP 工具，参数只接受 JSON 对象。', '默认只读；有状态写入需先 take。wait=true 不阻塞终端 UI；等待超时或断连不取消已接受操作。', 'mcp state {"wait_for_new":true}'),
}

PARAMETERS = {
    '字段': ('str', '点分字段路径，例如 frequency_hz。'),
    'JSON': ('JSON', '有效 JSON 值；字符串必须带双引号。'),
    'key': ('str', '共享状态键名。'),
    '设置项': ('str', '策略声明的设置项；可在 policy help <实例> 查看提示。'),
    '原始值': ('str', '不预先解析或重新序列化，原样交给 configure。'),
    '实例': ('str', '确切已加载实例 ID；同一来源可有 example-0、example-1。'),
    'id': ('str', '策略来源目录名，或原生链 ID。'),
    '名称或路径': ('str', '已保存姿态名称或可读状态文件路径。'),
    '名称': ('str', '目标名称；connector switch 使用 connector list 中的配置名称。'),
    '命令路径': ('str', '完整命令 token，例如 config rc set；不接受单词的截断前缀。'),
    '来源或实例': ('str', '来源 ID 展示其所有运行实例，实例 ID 只展示该实例。'),
    '命令': ('str', 'manifest 中声明的自定义命令名。'),
    '工具名': ('str', 'mcp help 目录中的工具名称。'),
    '工具': ('str', '已注册 MCP 工具名称。'),
    'JSON对象': ('object', 'MCP 工具参数；字段见 mcp help <工具名>。'),
    '参数…': ('list', '按声明顺序提供参数；含空格的文本使用引号。'),
    '--reset': ('flag', '切换时额外执行 RC 复位。'),
}


def builtin_help(path=()):
    tokens = tuple(path.split()) if isinstance(path, str) else tuple(path)
    selected = [(name, usage) for name, usage in COMMAND_TREE
                if tuple(name.split()[:len(tokens)]) == tokens]
    if not selected:
        raise ValueError('未知帮助主题；请使用完整命令路径')
    exact = next((row for row in selected if tuple(row[0].split()) == tokens), None)
    if not tokens or exact is None:
        value = document('RC 命令帮助' if not tokens else ' '.join(tokens) + ' 命令帮助',
            'help <完整命令路径> 查看参数与示例；policy help 查看策略，mcp help 查看工具。')
        for name, usage in selected:
            group, summary, _, _ = BUILTINS[name]
            value['commands'].append({'group': group, 'command': ' '.join(filter(None, (name, usage))), 'summary': summary})
        return value
    name, usage = exact
    _, summary, effect, example = BUILTINS[name]
    parameters = []
    for opening, key in re.findall(r'([<\[])((?:[^>\]])+)[>\]]', name + ' ' + usage):
        kind, description = PARAMETERS.get(key, ('str', '按用法提供此参数。'))
        parameters.append({'name': key, 'type': kind, 'required': opening == '<', 'description': description})
    return document(name, summary, usage=[' '.join(filter(None, (name, usage)))],
        sections=[{'title': '执行影响', 'body': effect}], parameters=parameters,
        commands=[{'group': '子命令', 'command': ' '.join(filter(None, row)), 'summary': BUILTINS[row[0]][1]}
                  for row in selected if row != exact],
        examples=[{'title': '调用示例', 'command': example}])
