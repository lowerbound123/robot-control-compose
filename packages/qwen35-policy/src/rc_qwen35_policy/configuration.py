"""Validate independent model files before an existing worker is unloaded."""
import copy
import json
import math
from pathlib import Path

from .statistics import SUPPORTED_SCHEMAS, read_action_stats


DEFAULTS = {
    'model': {
        'name': 'qwen3_5_vla_ki_wan', 'dtype': 'bfloat16', 'action_mode': 'delta',
        'stats_key': 'arx_acone', 'schema_key': 'arx_acone', 'state_offsets': {},
        'inference_backend': 'optimized', 'action_loss_only': True,
        'compile_model': False, 'compile_mode': 'reduce-overhead', 'warmup_runs': 2,
        'image_height': 224, 'image_width': 224,
        'subtask_refresh_chunks': 3, 'subtask_max_new_tokens': 128,
    },
    'runtime': {},
    'execution': {'chunk_size': 50, 'request_when_remaining': 30, 'inference_timeout_s': 60.0},
    'actions': {'processor': 'qwen35_single_grippers'},
}


def resolve_config_path(path, project_root):
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise ValueError('model config path must be nonempty')
    result = Path(path).expanduser()
    if not result.is_absolute():
        result = Path(project_root) / result
    return result.resolve()


def _read_json(path):
    with Path(path).open(encoding='utf-8') as stream:
        value = json.load(stream)
    json.dumps(value, allow_nan=False)
    if not isinstance(value, dict):
        raise ValueError(f'expected a JSON object: {path}')
    return value


def _positive(value, name, *, integer=False):
    kinds = (int,) if integer else (int, float)
    if type(value) not in kinds or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be positive and finite')


def load_model_config(path, *, workflow, project_root):
    """Resolve a complete JSON file; no inheritance from the active model."""
    if workflow not in ('single', 'two_stage'):
        raise ValueError('unsupported policy workflow')
    path = resolve_config_path(path, project_root)
    raw = _read_json(path)
    if set(raw) - set(DEFAULTS):
        raise ValueError('unknown model config section')
    model = raw.get('model')
    if not isinstance(model, dict) or not model.get('checkpoint'):
        raise ValueError('each model config must explicitly specify checkpoint')
    if model.get('workflow') != workflow:
        raise ValueError(f'model workflow must be {workflow!r} for this policy')
    config = copy.deepcopy(DEFAULTS)
    for section, values in raw.items():
        if not isinstance(values, dict):
            raise ValueError(f'{section} must be an object')
        allowed = set(DEFAULTS[section]) | ({'checkpoint', 'workflow'} if section == 'model' else
                                          {'instruction'} if section == 'runtime' else set())
        if set(values) - allowed:
            raise ValueError(f'unknown {section} setting: {sorted(set(values) - allowed)}')
        config[section].update(values)
    model = config['model']
    if model['name'] != 'qwen3_5_vla_ki_wan':
        raise ValueError('only qwen3_5_vla_ki_wan is supported')
    if not isinstance(model['checkpoint'], str):
        raise ValueError('checkpoint must be a path string')
    checkpoint = Path(model['checkpoint']).expanduser()
    if not checkpoint.is_absolute():
        checkpoint = path.parent / checkpoint
    checkpoint = checkpoint.resolve()
    checkpoint_config = _read_json(checkpoint / 'config.json')
    if checkpoint_config.get('type') != 'qwen3_5vla_ki_wan':
        raise ValueError('checkpoint type must be qwen3_5vla_ki_wan')
    if type(checkpoint_config.get('chunk_size')) is not int or checkpoint_config['chunk_size'] != 50:
        raise ValueError('checkpoint chunk_size must be exactly 50')
    if not (checkpoint / 'model.safetensors').is_file():
        raise FileNotFoundError(f'checkpoint requires single-file model.safetensors: {checkpoint}')
    model['checkpoint'] = str(checkpoint)
    if model['dtype'] not in ('bfloat16', 'float32', 'float16'):
        raise ValueError('unsupported model dtype')
    if model['inference_backend'] != 'optimized' or model['action_loss_only'] is not True:
        raise ValueError('local Qwen policy requires optimized action-only inference')
    if type(model['compile_model']) is not bool:
        raise ValueError('compile_model must be boolean')
    if model['action_mode'] not in ('delta', 'absolute'):
        raise ValueError('action_mode must be delta or absolute')
    for name in ('stats_key', 'schema_key', 'compile_mode'):
        if not isinstance(model[name], str) or not model[name].strip():
            raise ValueError(f'{name} must be a nonempty string')
    if model['schema_key'] not in SUPPORTED_SCHEMAS:
        raise ValueError(f'unsupported dual-arm three-camera schema: {model["schema_key"]}')
    read_action_stats(checkpoint, model['stats_key'])
    for name in ('image_height', 'image_width', 'subtask_refresh_chunks', 'subtask_max_new_tokens'):
        _positive(model[name], name, integer=True)
    if type(model['warmup_runs']) is not int or model['warmup_runs'] < 0:
        raise ValueError('warmup_runs must be a nonnegative integer')
    if not isinstance(model['state_offsets'], dict):
        raise ValueError('state_offsets must be an object')
    for key, value in model['state_offsets'].items():
        if not key.isdigit() or not 0 <= int(key) < 14 or type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('state_offsets requires indices 0..13 and finite offsets')
    instruction = config['runtime'].get('instruction')
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError('runtime.instruction must be a nonempty string')
    execution = config['execution']
    for key, expected in (('chunk_size', 50), ('request_when_remaining', 30)):
        if type(execution[key]) is not int or execution[key] != expected:
            raise ValueError(f'{key} must be {expected}')
    _positive(execution['inference_timeout_s'], 'inference_timeout_s')
    if config['actions']['processor'] != 'qwen35_single_grippers':
        raise ValueError('both supported deploydev presets use qwen35_single_grippers')
    return config


def validate_manifest(config):
    execution = config.get('execution', {})
    if (execution.get('mode') != 'sync' or execution.get('call_every_actions') != 1
            or execution.get('control_mode') != 'qpos'):
        raise ValueError('Qwen requires sync callbacks every complete qpos action')
    if config.get('hot_reloadable'):
        raise ValueError('local Qwen configuration is cold; use switch-model')
    path = config.get('params', {}).get('model_config_path')
    if not isinstance(path, str) or not path.strip():
        raise ValueError('params.model_config_path must be a nonempty string')
