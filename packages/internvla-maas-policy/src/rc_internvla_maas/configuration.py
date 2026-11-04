"""Cold configuration validation without importing network or model libraries."""
import copy
import json
import math
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit


DEFAULTS = {
    'service': {'model': 'internvla_a15', 'connect_timeout_s': 30.0, 'close_timeout_s': 2.0},
    'runtime': {},
    'observations': {'state_offsets': {}},
    'execution': {'chunk_size': 50, 'request_when_remaining': 30,
                  'inference_timeout_s': 60.0, 'expected_control_hz': 30.0},
    'actions': {'gripper_compensation': True},
}
EXTRA = {'service': {'address', 'robot_id', 'api_key', 'embodiment'},
         'runtime': {'instruction'}, 'observations': set(), 'execution': set(), 'actions': set()}


def resolve_config_path(path, project_root):
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise ValueError('model config path must be nonempty')
    result = Path(path).expanduser()
    return (result if result.is_absolute() else Path(project_root) / result).resolve()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _positive(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be positive and finite')


def _text(value, name, *, multiline=False):
    if (not isinstance(value, str) or not value.strip()
            or (not multiline and any(ord(char) < 32 or ord(char) == 127 for char in value))):
        raise ValueError(f'{name} must be nonempty text without control characters')


def load_model_config(path, *, project_root):
    path = resolve_config_path(path, project_root)
    try:
        raw = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_pairs)
        json.dumps(raw, allow_nan=False)
    except (OSError, UnicodeError, ValueError):
        raise ValueError('cannot read valid model configuration JSON') from None
    if not isinstance(raw, dict) or set(raw) - set(DEFAULTS):
        raise ValueError('unknown or invalid model configuration sections')
    config = copy.deepcopy(DEFAULTS)
    for section, values in raw.items():
        if not isinstance(values, dict) or set(values) - (set(DEFAULTS[section]) | EXTRA[section]):
            raise ValueError(f'unknown or invalid {section} settings')
        config[section].update(values)
    service = config['service']
    for key in ('address', 'model', 'robot_id', 'api_key', 'embodiment'):
        _text(service.get(key), f'service.{key}')
    address = service['address']
    try:
        parsed = urlsplit(address)
        valid = (address == address.strip() and parsed.scheme in ('ws', 'wss')
                 and parsed.hostname and parsed.port != 0 and parsed.username is None
                 and parsed.password is None and not parsed.query and not parsed.fragment
                 and '\\' not in unquote(parsed.path)
                 and not any(p in ('.', '..') for p in unquote(parsed.path).split('/')))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('service.address must be a ws/wss base address without credentials, query or fragment')
    for key in ('connect_timeout_s', 'close_timeout_s'):
        _positive(service[key], f'service.{key}')
    _text(config['runtime'].get('instruction'), 'runtime.instruction', multiline=True)
    execution = config['execution']
    for key, expected in (('chunk_size', 50), ('request_when_remaining', 30)):
        if type(execution[key]) is not int or execution[key] != expected:
            raise ValueError(f'execution.{key} must be {expected}')
    for key in ('inference_timeout_s', 'expected_control_hz'):
        _positive(execution[key], f'execution.{key}')
    offsets = config['observations']['state_offsets']
    if not isinstance(offsets, dict):
        raise ValueError('observations.state_offsets must be an object')
    for key, value in offsets.items():
        if (not isinstance(key, str) or not re.fullmatch(r'(?:[0-9]|1[0-3])', key)
                or type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 3.4e38):
            raise ValueError('state_offsets requires indices 0..13 and finite float32 offsets')
    if type(config['actions']['gripper_compensation']) is not bool:
        raise ValueError('actions.gripper_compensation must be boolean')
    return config


def validate_manifest(config):
    execution = config.get('execution', {})
    if (execution.get('mode') != 'sync' or execution.get('call_every_actions') != 1
            or execution.get('control_mode') != 'qpos'):
        raise ValueError('MaaS requires sync callbacks every complete qpos action')
    if config.get('hot_reloadable'):
        raise ValueError('MaaS configuration is cold; use switch-model')
    _text(config.get('params', {}).get('model_config_path'), 'params.model_config_path')
