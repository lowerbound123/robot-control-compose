"""Static, standard-library-only UI hints; never an execution validator."""
import math
from .help_metadata import help_string, normalize_help_details

PRIMITIVES = frozenset(('str', 'int', 'float', 'bool'))
PARAMETER_TYPES = PRIMITIVES | {'enum', 'path', 'json'}
_FIELDS = {'name', 'type', 'help', 'choices', 'root', 'kind', 'suffixes'}


def _text(value, field, maximum, *, empty=True):
    help_string(value, field, maximum)
    if not empty and not value.strip():
        raise ValueError(f'{field} must be a bounded string')
    return value


def normalize_parameter(raw, *, name=None):
    if type(raw) is str:
        raw = {'type': raw}
    if type(raw) is not dict or set(raw) - _FIELDS:
        raise ValueError('invalid parameter declaration')
    kind = raw.get('type')
    if type(kind) is not str or kind not in PARAMETER_TYPES:
        raise ValueError('parameter type must be str/int/float/bool/enum/path/json')
    result = {'name': _text(raw.get('name', name if name is not None else kind), 'name', 256, empty=False),
              'type': kind, 'help': _text(raw.get('help', ''), 'help', 4096)}
    if 'choices' in raw or kind == 'enum':
        choices = raw.get('choices')
        if type(choices) is not list or not choices or len(choices) > 200:
            raise ValueError('choices must contain 1 to 200 values')
        for value in choices:
            valid = (type(value) is str and len(value) <= 4096 and '\0' not in value if kind in ('str', 'enum', 'path', 'json') else
                     type(value) is int if kind == 'int' else type(value) is bool if kind == 'bool' else
                     type(value) in (int, float) and math.isfinite(value))
            if not valid:
                raise ValueError('choice type differs from parameter type')
        result['choices'] = list(choices)
    if kind == 'path':
        result['root'] = _text(raw.get('root', '.'), 'root', 4096, empty=False)
        result['kind'] = raw.get('kind', 'any')
        if result['kind'] not in ('any', 'file', 'directory'):
            raise ValueError('path kind must be any/file/directory')
        suffixes = raw.get('suffixes', [])
        if type(suffixes) is not list or len(suffixes) > 64 or any(
                type(item) is not str or not item.startswith('.') or '/' in item or '\0' in item or len(item) > 128 for item in suffixes):
            raise ValueError('suffixes must be bounded dot-prefixed extensions')
        result['suffixes'] = list(suffixes)
    elif {'root', 'kind', 'suffixes'} & raw.keys():
        raise ValueError('root/kind/suffixes require path type')
    return result


def executable_type(parameter):
    return parameter['type'] if parameter['type'] in PRIMITIVES else 'str'


def normalize_config_hints(raw=None):
    if raw is None:
        raw = {}
    if type(raw) is not dict or set(raw) - {'help', 'help_details', 'settings'}:
        raise ValueError('config_hints must contain help/help_details/settings')
    settings = raw.get('settings', {})
    if type(settings) is not dict or len(settings) > 256:
        raise ValueError('config_hints.settings must be a bounded object')
    result = {'help': _text(raw.get('help', ''), 'config_hints.help', 4096), 'settings': {}}
    if 'help_details' in raw:
        result['help_details'] = normalize_help_details(raw['help_details'])
    for key, value in settings.items():
        _text(key, 'setting key', 256, empty=False)
        if any(character.isspace() for character in key):
            raise ValueError('setting keys cannot contain whitespace')
        result['settings'][key] = normalize_parameter(value, name=key)
    return result


def path_parameter(config, selector):
    """Resolve only declared paths; a caller can never supply its own root."""
    if type(selector) is not dict:
        raise ValueError('invalid path selector')
    if set(selector) == {'setting'} and type(selector['setting']) is str:
        parameter = normalize_config_hints(config.get('config_hints'))['settings'].get(selector['setting'])
    elif set(selector) == {'command', 'argument'} and type(selector['command']) is str and type(selector['argument']) is int:
        from .commands import command_specs
        command = command_specs(config).get(selector['command'])
        index = selector['argument']
        parameter = command['parameters'][index] if command and 0 <= index < len(command['parameters']) else None
    else:
        raise ValueError('invalid path selector')
    if parameter is None or parameter['type'] != 'path':
        raise ValueError('selector does not name a declared path parameter')
    return parameter
