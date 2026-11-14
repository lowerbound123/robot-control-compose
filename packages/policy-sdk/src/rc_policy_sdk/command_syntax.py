"""Shared human command grammar. Standard library only; no execution or I/O."""
from dataclasses import dataclass
import json
import shlex

# Each entry is (literal path, argument usage). One source for help/reservation/completion.
COMMAND_TREE = (
    ('help', '[命令路径]'), ('status', ''), ('pause', ''), ('resume', ''), ('reset', ''), ('exit', ''),
    ('connector list', ''), ('connector status', ''), ('connector switch', '<名称>'), ('connector retry', ''),
    ('config rc show', ''), ('config rc set', '<字段> <JSON>'), ('config rc save', ''),
    ('global show', '[key]'), ('global set', '<key> <JSON>'), ('global remove', '<key>'),
    ('pose save', '<名称>'), ('pose restore', '<名称或路径>'), ('snapshot', ''),
    ('record', ''), ('record start', ''), ('record stop', ''), ('record status', ''),
    ('record auto', '[on|off]'),
    ('record reduce', '<日志文件夹名|all>'),
    ('camera', ''), ('camera show', ''), ('camera hide', ''), ('camera toggle', ''), ('monitor', '[实例]'),
    ('policy help', '[来源或实例] [命令]'), ('policy list', ''), ('policy switch', '<id> [--reset]'),
    ('policy manage', ''), ('policy manage apply', ''), ('policy manage save', ''), ('policy manage close', ''),
    ('policy <实例> config show', ''), ('policy <实例> config set', '<设置项> <原始值>'),
    ('policy <实例> config save', ''), ('policy <实例> retry', ''), ('policy <实例> <命令>', '[参数…]'),
    ('mcp help', '[工具名]'), ('mcp take', '[JSON对象]'), ('mcp return', '[JSON对象]'),
    ('mcp <工具>', '[JSON对象]'),
)
POLICY_ROOT_RESERVED = frozenset({'help', 'list', 'switch', 'manage'})
POLICY_COMMAND_RESERVED = frozenset(word for path, _ in COMMAND_TREE for word in path.split() if not word.startswith('<'))
ROOT_COMMANDS = tuple(dict.fromkeys(path.split()[0] for path, _ in COMMAND_TREE))
READ_KINDS = frozenset({'help', 'status', 'rc_config_show', 'global_show', 'policy_help', 'policy_list', 'policy_config_show'})
LOCAL_KINDS = frozenset({'camera', 'monitor', 'snapshot', 'policy_manage', 'mcp_help', 'mcp_tool'})


@dataclass(frozen=True)
class Command:
    kind: str
    target: str | None = None
    args: tuple = ()
    value: str | None = None
    key: str | None = None

    @property
    def readonly(self):
        return (self.kind in READ_KINDS or self.kind == 'record' and self.args == ('status',)
                or self.kind == 'connector' and self.args in (('list',), ('status',)))

    @property
    def local_only(self):
        return self.kind in LOCAL_KINDS


def command_help():
    return '\n'.join(' '.join(filter(None, row)) for row in COMMAND_TREE)


def _json_value(text):
    def invalid(value):
        raise ValueError(f'JSON 不支持 {value}')
    return json.loads(text, parse_constant=invalid)


def _json_object(text):
    value = _json_value(text)
    if not isinstance(value, dict):
        raise ValueError('MCP 参数必须是 JSON 对象')
    return value


def parse_command(line: str) -> Command:
    if not isinstance(line, str) or not line.strip():
        raise ValueError('命令不能为空')
    raw = line.lstrip()
    # Do not shlex the value tail: configuration handlers own its interpretation.
    for prefix, count, kind in (('config rc set ', 4, 'rc_config_set'), ('global set ', 3, 'global_set')):
        tokens = raw.split(maxsplit=count)
        expected = prefix.split()
        if tokens[:len(expected)] == expected:
            if len(tokens) != count + 1:
                raise ValueError('用法：' + prefix + '<字段> <值>')
            _json_value(tokens[count])
            return Command(kind, target=tokens[count - 1], value=tokens[count])
    tokens = raw.split(maxsplit=5)
    if len(tokens) >= 4 and tokens[0] == 'policy' and tokens[2:4] == ['config', 'set']:
        if tokens[1] in POLICY_ROOT_RESERVED or len(tokens) != 6:
            raise ValueError('用法：policy <实例> config set <设置项> <值>')
        return Command('policy_config_set', target=tokens[1], key=tokens[4], value=tokens[5])
    if raw.split(maxsplit=1)[0] == 'mcp':
        parts = raw.split(maxsplit=2)
        if len(parts) < 2:
            raise ValueError('请使用 mcp help 或 mcp <工具> [JSON对象]')
        name = parts[1]
        if name == 'help':
            args = shlex.split(parts[2]) if len(parts) == 3 else []
            if len(args) > 1:
                raise ValueError('用法：mcp help [工具名]')
            return Command('mcp_help', args[0] if args else None)
        payload = _json_object(parts[2]) if len(parts) == 3 else {}
        return Command('mcp_tool', name, args=(payload,))
    words = shlex.split(raw)
    head, args = words[0], words[1:]
    if head in ('status', 'pause', 'resume', 'reset', 'exit', 'snapshot') and not args:
        return Command(head)
    if head == 'connector' and (args in (['list'], ['status'], ['retry']) or
                              len(args) == 2 and args[0] == 'switch'):
        return Command('connector', args=tuple(args))
    if head == 'help':
        if len(args) == 1 and args[0] in ('mcp', 'policy'):
            raise ValueError(f'请使用 {args[0]} help')
        if args and not any(path.split()[:len(args)] == args for path, _ in COMMAND_TREE):
            raise ValueError('未知帮助主题；策略帮助使用 policy help <实例>')
        return Command('help', args=tuple(args))
    if head == 'config' and args in (['rc', 'show'], ['rc', 'save']):
        return Command('rc_config_' + args[1])
    if head == 'global' and args:
        if args[0] == 'show' and len(args) in (1, 2):
            return Command('global_show', args[1] if len(args) == 2 else None)
        if args[0] == 'remove' and len(args) == 2:
            return Command('global_remove', args[1])
    if head == 'pose' and len(args) == 2 and args[0] in ('save', 'restore'):
        return Command('pose_' + args[0], args[1])
    if head == 'record' and (not args or args in (['start'], ['stop'], ['status'], ['auto'], ['auto','on'], ['auto','off'])
                             or len(args) == 2 and args[0] == 'reduce'):
        return Command('record', args=tuple(args))
    if head == 'camera' and (not args or args in (['show'], ['hide'], ['toggle'])):
        return Command('camera', args=tuple(args or ['toggle']))
    if head == 'monitor' and len(args) <= 1:
        if args and args[0] in ('on', 'off', 'show', 'hide', 'toggle'):
            raise ValueError('用法：monitor [实例]；不带实例切换机器人面板')
        return Command('monitor', args[0] if args else None)
    if head == 'policy' and args:
        first = args[0]
        if first == 'help' and len(args) <= 3:
            return Command('policy_help', args[1] if len(args) > 1 else None, tuple(args[2:]))
        if first == 'list' and len(args) == 1:
            return Command('policy_list')
        if first == 'switch' and (len(args) == 2 or len(args) == 3 and args[2] == '--reset'):
            return Command('policy_switch', args[1], tuple(args[2:]))
        if first == 'manage' and (len(args) == 1 or len(args) == 2 and args[1] in ('apply', 'save', 'close')):
            return Command('policy_manage', args=tuple(args[1:]))
        if first not in POLICY_ROOT_RESERVED and len(args) >= 2:
            if args[1] == 'config' and len(args) == 3 and args[2] in ('show', 'save'):
                return Command('policy_config_' + args[2], first)
            if args[1] == 'retry' and len(args) == 2:
                return Command('policy_retry', first)
            if args[1] not in POLICY_COMMAND_RESERVED and args[1] not in ('command', 'runtime'):
                return Command('policy_command', first, tuple(args[1:]))
    replacements = {'r':'reset', 'quit':'exit', 'take':'mcp take', 'return':'mcp return',
        'switch':'policy switch', 'policies':'policy list', 'save-config':'config rc save / policy <实例> config save',
        'manage-policy':'policy manage', 'retry-reload':'policy <实例> retry',
        'save':'pose save', 'load':'pose restore', 'set':'config rc set / policy <实例> config set / global set',
        'show':'config rc show / policy <实例> config show / global show', 'unset':'global remove'}
    if head in ROOT_COMMANDS:
        usage = '\n'.join(' '.join(filter(None, row)) for row in COMMAND_TREE if row[0].split()[0] == head)
        raise ValueError('用法：' + usage)
    hint = replacements.get(head, 'help 查看正式命令；自定义命令使用 policy <实例> <命令>')
    raise ValueError(f'不支持此命令或参数；请使用 {hint}')


def complete_command(prefix, *, instances=(), policy_commands=None, tools=()):
    candidates = set()
    for path, _ in COMMAND_TREE:
        if '<实例>' in path:
            for identity in instances:
                expanded = path.replace('<实例>', identity)
                if '<命令>' in expanded:
                    for name in (policy_commands or {}).get(identity, ()):
                        candidates.add(expanded.replace('<命令>', name))
                else:
                    candidates.add(expanded)
        elif '<工具>' in path:
            candidates.update(path.replace('<工具>', name) for name in tools)
        elif '<' not in path:
            candidates.add(path)
    candidates.update('monitor ' + identity for identity in instances)
    candidates.update(('record auto on', 'record auto off'))
    return sorted(value for value in candidates if value.startswith(prefix))
