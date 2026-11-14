"""Pure, tolerant command completion. No parser calls, execution, or filesystem I/O."""
from dataclasses import dataclass, field
import json
import math
import shlex

from rc_policy_sdk.command_syntax import COMMAND_TREE, ROOT_COMMANDS


@dataclass(frozen=True)
class CompletionItem:
    label: str
    insert_text: str
    help: str = ''
    kind: str = ''


@dataclass
class CompletionResult:
    items: list
    start: int
    end: int
    hint: str = ''
    lint: str = ''
    path_request: dict | None = None
    text: str = ''
    cursor: int = 0
    revision: int = 0
    _quote: str = field(default='', repr=False)
    _closed_quote: bool = field(default=False, repr=False)
    _raw: bool = field(default=False, repr=False)
    _value_type: str = field(default='', repr=False)


@dataclass(frozen=True)
class _Token:
    start: int
    end: int
    value: str


def _decode_state(raw):
    value, quote, escaped = [], '', False
    for char in raw:
        if escaped:
            # Match Python shlex (the execution parser), not shell expansion.
            if quote == '"' and char not in '\\"':
                value.append('\\')
            value.append(char)
            escaped = False
        elif char == '\\' and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = ''
            else:
                value.append(char)
        elif char in ('"', "'"):
            quote = char
        else:
            value.append(char)
    if escaped:
        value.append('\\')
    return ''.join(value), quote


def _decode(raw):
    return _decode_state(raw)[0]


def _tokens(text):
    result, position = [], 0
    while position < len(text):
        if text[position].isspace():
            position += 1
            continue
        start, quote, escaped = position, '', False
        while position < len(text):
            char = text[position]
            if escaped:
                escaped = False
            elif char == '\\' and quote != "'":
                escaped = True
            elif quote:
                if char == quote:
                    quote = ''
            elif char in ('"', "'"):
                quote = char
            elif char.isspace():
                break
            position += 1
        result.append(_Token(start, position, _decode(text[start:position])))
    return result


def _names(values):
    if isinstance(values, dict):
        return list(values)
    return [value.get('name', '') if isinstance(value, dict) else str(value) for value in values]


def _paths(catalog):
    instances = catalog.get('instances', {})
    for path, usage in COMMAND_TREE:
        words = path.split()
        if '<实例>' in words:
            for identity, entry in instances.items():
                concrete = [identity if word == '<实例>' else word for word in words]
                if '<命令>' in concrete:
                    for name, spec in entry.get('commands', {}).items():
                        yield [name if word == '<命令>' else word for word in concrete], spec.get('help', '')
                else:
                    yield concrete, usage
        elif '<工具>' in words:
            for name in _names(catalog.get('tools', ())):
                yield [name if word == '<工具>' else word for word in words], usage
        else:
            yield words, usage


def _leaves(value, prefix=''):
    result = {}
    for key, item in value.items():
        if key == 'api_key':
            continue
        path = f'{prefix}.{key}' if prefix else key
        if isinstance(item, dict):
            result.update(_leaves(item, path))
        else:
            result[path] = item
    return result


def _parameters(spec):
    if 'parameters' in spec:
        return spec['parameters']
    return [{'name': f'arg{index + 1}', 'type': value} for index, value in enumerate(spec.get('args', ()))]


def _lint(value, spec):
    if not value:
        return ''
    kind = spec.get('type', 'str')
    try:
        if kind == 'int':
            int(value)
        elif kind == 'float':
            if not math.isfinite(float(value)):
                raise ValueError()
        elif kind == 'bool' and value not in ('true', 'false'):
            raise ValueError()
        elif kind == 'enum' and value not in [str(choice) for choice in spec.get('choices', ())]:
            return '值不在建议选项中；提示不阻止提交'
        elif kind == 'json':
            json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError):
        return f'当前值不是有效的 {kind}；提示不阻止提交'
    return ''


def analyze(text, cursor, catalog):
    """Return current-token candidates/ranges (Python Unicode code-point offsets)."""
    cursor = min(max(cursor, 0), len(text))
    tokens = _tokens(text)
    current = next((i for i, token in enumerate(tokens) if token.start <= cursor <= token.end), None)
    if current is None:
        current = sum(token.end < cursor for token in tokens)
        start = end = cursor
    else:
        start, end = tokens[current].start, tokens[current].end
    words = [token.value for token in tokens]
    previous = words[:current]
    raw_tail = None
    if words[:1] == ['policy'] and words[2:4] == ['config', 'set'] and current >= 5:
        raw_tail = 5
    elif words[:3] == ['config', 'rc', 'set'] and current >= 4:
        raw_tail = 4
    elif words[:2] == ['global', 'set'] and current >= 3:
        raw_tail = 3
    elif words[:1] == ['mcp'] and len(words) > 1 and words[1] != 'help' and current >= 2:
        raw_tail = 2
    if raw_tail is not None:
        current = raw_tail
        previous = words[:current]
        start = tokens[current].start if len(tokens) > current else cursor
        end = len(text)
    raw = text[start:end]
    quote = raw[:1] if raw[:1] in ('"', "'") else ''
    prefix = _decode(text[start:cursor]) if quote or raw_tail is None else text[start:cursor]
    result = CompletionResult([], start, end, text=text, cursor=cursor, _quote=quote,
                              _closed_quote=bool(quote and len(raw) > 1 and not _decode_state(raw)[1]),
                              _raw=raw_tail is not None)
    choices = {}
    declared_order = False
    for path, description in _paths(catalog):
        if len(path) > current and path[:current] == previous:
            choices[path[current]] = CompletionItem(path[current], path[current], description, 'command')
    spec, path_request = None, None
    instances = catalog.get('instances', {})
    source_names = _names(catalog.get('sources', ()))
    if previous == ['policy', 'switch']:
        choices.update({name: CompletionItem(name, name, kind='policy') for name in source_names})
    elif previous[:2] == ['policy', 'switch'] and current == 3:
        choices['--reset'] = CompletionItem('--reset', '--reset')
    elif previous == ['policy', 'help']:
        choices.update({name: CompletionItem(name, name, kind='policy') for name in set(source_names) | set(instances)})
    elif previous[:2] == ['policy', 'help'] and current == 3:
        entry = instances.get(previous[2], {})
        choices.update({name: CompletionItem(name, name, item.get('help', ''), 'command')
                        for name, item in entry.get('commands', {}).items()})
    elif previous == ['monitor']:
        choices.update({name: CompletionItem(name, name, kind='policy')
                        for name, item in instances.items() if item.get('monitor', True)})
    elif previous == ['mcp', 'help']:
        choices.update({name: CompletionItem(name, name, kind='tool') for name in _names(catalog.get('tools', ()))})
    elif previous[:1] == ['mcp'] and current == 2 and previous[1] != 'help':
        spec = {'name': 'arguments', 'type': 'json', 'help': '工具参数 JSON 对象'}
    elif previous == ['help']:
        choices.update({name: CompletionItem(name, name) for name in ROOT_COMMANDS if name not in ('mcp', 'policy')})
    elif previous[:2] == ['config', 'rc'] and len(previous) >= 3 and previous[2] == 'set':
        leaves = _leaves(catalog.get('rc_config', {}))
        if current == 3:
            choices.update({name: CompletionItem(name, name, kind='setting') for name in leaves})
        elif current == 4:
            value = leaves.get(previous[3])
            spec = {'name': previous[3], 'type': 'json', 'help': 'RC 草稿；保存后重启生效'}
            if isinstance(value, bool):
                choices.update({value: CompletionItem(value, value, kind='bool') for value in ('true', 'false')})
    elif previous in (['global', 'show'], ['global', 'remove'], ['global', 'set']):
        choices.update({name: CompletionItem(name, name, kind='key') for name in catalog.get('global_keys', ())})
    elif previous[:2] == ['global', 'set'] and current == 3:
        spec = {'name': 'value', 'type': 'json'}
    elif previous == ['pose', 'save']:
        spec = {'name': 'name', 'type': 'str', 'help': '保存姿态的名称'}
    elif previous == ['pose', 'restore']:
        spec = {'name': 'pose', 'type': 'path', 'root': '.', 'kind': 'file', 'suffixes': ['.json']}
        path_request = {'instance': None, 'spec': spec, 'base_dir': catalog.get('pose_root', '.'), 'pose': True}
    elif previous == ['record', 'auto']:
        choices.update({value: CompletionItem(value, value, '自动换段开关；off 保留当前录制') for value in ('on', 'off')})
    elif previous == ['record', 'reduce']:
        choices['all'] = CompletionItem('all', 'all', '处理全部静态日志，跳过活动录制，可处理异常停止会话')
        spec = {'name': '日志文件夹名或 all', 'type': 'path', 'root': '.', 'kind': 'directory',
                'help': '转视频并校验后删除原图；保留 initial.png 和其他日志'}
        path_request = {'instance': None, 'spec': spec, 'base_dir': catalog.get('logs_root', '.'), 'record_reduce': True}
    elif previous[:1] == ['policy'] and current >= 2 and previous[1] in instances:
        identity, entry = previous[1], instances[previous[1]]
        settings = entry.get('config_hints', {}).get('settings', {})
        if previous[2:4] == ['config', 'set']:
            if current == 4:
                choices.update({key: CompletionItem(key, key, value.get('help', ''), 'setting') for key, value in settings.items()})
                result.hint = entry.get('config_hints', {}).get('help', '')
            elif current == 5:
                key = previous[4]
                spec = settings.get(key)
                if spec is None:
                    result.lint = '未在提示声明中；原始值仍交由 policy 处理'
                else:
                    spec = dict(spec, name=spec.get('name', key))
                    path_request = {'instance': identity, 'selector': {'setting': key}}
        elif len(previous) >= 3:
            declared = entry.get('commands', {}).get(previous[2])
            if declared is not None:
                parameters = _parameters(declared)
                index = current - 3
                if 0 <= index < len(parameters):
                    spec = parameters[index]
                    path_request = {'instance': identity, 'selector': {'command': previous[2], 'argument': index}}
                elif index >= len(parameters):
                    result.lint = '参数数量超出命令声明；提示不阻止提交'
    if spec:
        kind = spec.get('type', 'str')
        result._value_type = kind
        result.hint = ' · '.join(str(part) for part in (spec.get('name', 'value'), kind, spec.get('help', '')) if part)
        value = raw if result._raw and kind == 'json' else _decode(raw) if quote or not result._raw else raw
        result.lint = _lint(value, spec)
        if 'choices' in spec or kind == 'bool':
            declared_order = True
            values = spec.get('choices', ('true', 'false'))
            values = [str(value).lower() if isinstance(value, bool) else str(value) for value in values]
            choices.update({value: CompletionItem(value, value, spec.get('help', ''), kind) for value in values})
        if kind == 'path' and path_request is not None:
            result.path_request = dict(path_request, prefix=prefix)
    if not choices and not spec and not result.lint and previous and previous[0] not in ROOT_COMMANDS:
        result.lint = '未知命令；请使用正式命令路径'
    ordered = choices.items() if declared_order else sorted(choices.items())
    result.items = [item for name, item in ordered if name.startswith(prefix)]
    return result


def apply_completion(text, cursor, result, item):
    """Replace exactly the analyzed argument, retaining its quote style/suffix."""
    if text != result.text or cursor != result.cursor:
        raise ValueError('补全结果已过期')
    value, directory = item.insert_text, item.kind == 'directory'
    quote = result._quote
    if quote == "'":
        replacement = "'" + value.replace("'", "'\\''")
        if not directory or result._closed_quote:
            replacement += "'"
    elif quote == '"':
        replacement = '"' + value.replace('\\', '\\\\').replace('"', '\\"')
        if not directory or result._closed_quote:
            replacement += '"'
    elif result._raw:
        quoted = any(char.isspace() for char in value) or value.startswith(('"', "'", '{', '['))
        if result._value_type in ('str', 'enum', 'path'):
            try:
                quoted = quoted or not isinstance(json.loads(value), str)
            except ValueError:
                pass
        replacement = json.dumps(value, ensure_ascii=False) if quoted else value
    else:
        replacement = shlex.quote(value)
    suffix = text[result.end:]
    position = result.start + len(replacement)
    if directory:
        if quote and result._closed_quote or not quote and replacement[:1] in ("'", '"'):
            position -= 1
    elif result._raw:
        # This is the final, policy-owned value tail. A separator here would
        # become part of bare text/path values received by configure().
        pass
    elif suffix[:1].isspace():
        position += 1
    else:
        replacement += ' '
        position += 1
    return text[:result.start] + replacement + suffix, position
