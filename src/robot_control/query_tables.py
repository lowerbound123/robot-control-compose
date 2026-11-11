"""Pure adapters for explicitly selected readonly builtin query results."""
import json
import re

from .secret_config import public_value

_IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
_MISSING = '—'


def _path(parent, key):
    if _IDENTIFIER.fullmatch(key):
        return parent + '.' + key if parent else key
    return parent + '[' + json.dumps(key, ensure_ascii=False) + ']'


def _fields(value, prefix=''):
    """Flatten nonempty objects only; arrays/empty objects remain full values."""
    if isinstance(value, dict) and value:
        for key, child in value.items():
            yield from _fields(child, _path(prefix, key))
    else:
        yield [prefix or '$', value]


def _table(title, columns, rows):
    return dict(title=title, columns=columns, rows=rows)


def _groups(title, result):
    top, groups = [], []
    for key, value in result.items():
        if isinstance(value, dict) and value:
            groups.append(_table(title + ' · ' + key, ['字段', '值'], list(_fields(value, _path('', key)))))
        else:
            top.append([_path('', key), value])
    return [_table(title, ['字段', '值'], top), *groups]


def query_table_document(command, result):
    """Return display metadata only; never query, import policies or mutate data.

    Callers publish through public_table_fields(result, document), preserving the
    business payload and the existing redaction boundary independently of UI.
    """
    if command not in ('connector list', 'policies', 'status', 'connector status', 'record status'):
        return None
    # Redact while object keys still carry their original meaning: flattening
    # first can hide sensitive ancestors behind bracket-escaped field paths.
    result = public_value(result)
    if command == 'connector list':
        keys = ['name', 'source', 'connector', 'valid', 'config', 'error']
        columns = ['名称', '来源', '实例', '有效', '配置路径', '错误']
        tables = [_table(command, columns, [[row.get(key, _MISSING) for key in keys] for row in result])]
        extra = [[str(index), path, value] for index, row in enumerate(result)
                 for path, value in _fields({key: value for key, value in row.items() if key not in keys})
                 if path != '$']
        if extra:
            tables.append(_table('connector list · 其他字段', ['行', '字段', '值'], extra))
    elif command == 'policies':
        keys = ['id', 'kind', 'state', 'control_mode', 'valid']
        columns = ['来源ID', '类型', '状态', '控制模式', '有效']
        rows = []
        for row in result:
            details = {key: value for key, value in row.items() if key not in keys}
            rows.append([*[row.get(key, _MISSING) for key in keys], details])
        tables = [_table('policy list', [*columns, '详情'], rows)]
    elif command == 'status':
        tables = _groups('status', result)
    elif command == 'connector status':
        instances = result.get('instances', {})
        base = {key: value for key, value in result.items() if key != 'instances' or not value or not isinstance(value, dict)}
        tables = _groups(command, base)
        rows = [[identity, path, value] for identity, instance in instances.items()
                for path, value in _fields(instance)] if isinstance(instances, dict) else []
        tables.append(_table('connector status · instances', ['实例', '字段', '值'], rows))
    elif command == 'record status':
        overview, pending, statistics = {}, {}, {}
        for key, value in result.items():
            target = overview if key in ('state', 'session_id', 'path', 'error') else pending if key.startswith('pending_') else statistics
            target[key] = value
        tables = [_table(command, ['字段', '值'], list(_fields(overview)))]
        if pending:
            tables.append(_table(command + ' · pending', ['字段', '值'], list(_fields(pending))))
        if statistics:
            tables.append(_table(command + ' · statistics', ['字段', '值'], list(_fields(statistics))))
    else:
        return None
    return {'schema_version': 1, 'tables': tables}
