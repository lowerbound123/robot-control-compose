"""Preflight the dev Pi05 export without importing Torch or a training repository."""
import copy
import json
import math
from pathlib import Path
import re

import numpy as np

from .gripper import parse_gripper_value

CAMERAS = ('head', 'left_wrist', 'right_wrist')
CAMERA_MAP = dict(zip(CAMERAS, ('observation.images.image0', 'observation.images.image1',
                              'observation.images.image2')))
GRIPPER_DEFAULTS = {'gripper_upper_threshold': 2.5, 'gripper_upper_value': 4.9,
                    'gripper_lower_threshold': 2.5, 'gripper_lower_value': 0.}
DEFAULTS = {
    'model': {'device': 'cuda', 'compile_model': False, 'warmup_runs': 1},
    'runtime': {},
    'observations': {'state_scale': [1.0] * 14, 'state_offset': [0.0] * 14},
    'actions': {},
    'execution': {'request_when_remaining': None, 'inference_timeout_s': 60.0},
}
EXTRA = {'model': {'checkpoint', 'tokenizer_path'}, 'runtime': {'instruction'},
         'observations': {'camera_map'}, 'actions': {'representation'}, 'execution': set()}


def resolve_config_path(path, project_root):
    if not isinstance(path, (str, Path)) or not str(path).strip():
        raise ValueError('model config path must be nonempty')
    value = Path(path).expanduser()
    return (value if value.is_absolute() else Path(project_root) / value).resolve()


def read_json(path):
    with Path(path).open(encoding='utf-8') as stream:
        value = json.load(stream)
    json.dumps(value, allow_nan=False)
    if not isinstance(value, dict):
        raise ValueError(f'expected JSON object: {path}')
    return value


def local_file(root, name):
    if (not isinstance(name, str) or not name or Path(name).is_absolute()
            or '..' in Path(name).parts):
        raise ValueError(f'checkpoint filename must be relative: {name!r}')
    path = Path(root) / name
    if not path.is_file() or not path.stat().st_size:
        raise FileNotFoundError(f'required checkpoint file missing or empty: {path}')
    return path


def _positive_int(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f'{name} must be a positive integer')
    return value


def _validate_training(checkpoint):
    dataset = read_json(local_file(checkpoint, 'train_config.json')).get('dataset', {})
    expected = [
        {'type': 'delta_action', 'mask': None, 'mapping': {}, 'robot_type': None, 'relative_pose_slices': None},
        {'type': 'resize_with_pad', 'height': 224, 'width': 224, 'mode': 'bilinear', 'mapping': {}},
        {'type': 'normalize', 'selected_keys': None, 'mode': 'mean_std', 'norm_stats': {}},
        {'type': 'compose_fields', 'mapping': {}},
        {'type': 'pi05_gemma_tokenizer', 'pretrained_model_name_or_path': 'google/paligemma-3b-pt-224',
         'max_length': 200, 'max_state_dim': 32, 'task_key': 'task', 'padding_side': 'right',
         'padding': 'max_length', 'truncation': True},
        {'type': 'pad_state_and_action', 'max_state_dim': 32, 'max_action_dim': 32},
        {'type': 'remap_image_key', 'mapping': {}},
        {'type': 'unify_pi05_inputs'},
    ]
    if (dataset.get('type') != 'pi05' or dataset.get('action_mode') != 'delta'
            or dataset.get('data_transforms') != {'inputs': expected, 'outputs': []}
            or any(dataset.get(key) != {'inputs': [], 'outputs': []}
                   for key in ('repack_transforms', 'model_transforms'))):
        raise ValueError('unsupported train_config pipeline: requires dev Pi05 mean_std, '
                         'contiguous 14 values padded to 32, and joint-only delta actions')


def _load_statistics(checkpoint):
    # Explicitly select this robot's statistics, never the ACOne statistics
    # stored alongside them. Order matches compose_fields, not action_reorder.
    data = read_json(local_file(checkpoint, 'stats.json')).get('arx_lift2')
    if not isinstance(data, dict):
        raise ValueError('stats.json requires arx_lift2 statistics')
    result = {}
    for output, prefix in (('state', 'states'), ('action', 'actions')):
        result[output] = {}
        for key in ('mean', 'std'):
            arrays = []
            for part, size in (('left_joint', 6), ('left_gripper', 1), ('right_joint', 6), ('right_gripper', 1)):
                name = f'{prefix}.{part}.position'
                with np.errstate(over='ignore', invalid='ignore'):
                    value = np.asarray(data.get(name, {}).get(key), dtype=np.float32)
                if (value.shape != (size,) or not np.isfinite(value).all()
                        or key == 'std' and (value < 0).any()):
                    raise ValueError(f'invalid statistic {name}.{key}: expected {size} finite values, std >= 0')
                arrays.extend(value.tolist())
            result[output][key] = arrays
    return result


def _validate_checkpoint(checkpoint):
    if checkpoint.get('type') != 'pi05':
        raise ValueError('checkpoint type must be pi05')
    for group, key, kind in (('input_features', 'observation.state', 'STATE'),
                             ('output_features', 'action', 'ACTION')):
        if checkpoint.get(group) != {key: {'type': kind, 'shape': [32]}}:
            raise ValueError(f'dev checkpoint {key} must declare 32 {kind} values')
    for key, expected in (('n_obs_steps', 1), ('max_state_dim', 32), ('max_action_dim', 32),
                          ('tokenizer_max_length', 200)):
        if type(checkpoint.get(key)) is not int or checkpoint[key] != expected:
            raise ValueError(f'dev checkpoint requires {key}={expected}')
    if checkpoint.get('image_resolution') != [224, 224] or checkpoint.get('empty_cameras', 0):
        raise ValueError('dev checkpoint requires three real 224x224 cameras')
    if checkpoint.get('rtc_config') is not None:
        raise ValueError('LeRobot RTC is unsupported; scheduling belongs to RC')
    if checkpoint.get('use_visual_memory') or checkpoint.get('use_proprioceptive_memory'):
        raise ValueError('history/memory checkpoints are unsupported')
    for key in ('paligemma_variant', 'action_expert_variant'):
        if checkpoint.get(key) not in ('gemma_2b', 'gemma_300m'):
            raise ValueError(f'unsupported {key}')
    if checkpoint.get('dtype') not in ('float32', 'bfloat16'):
        raise ValueError('dtype must be float32 or bfloat16')
    _positive_int(checkpoint.get('num_inference_steps'), 'num_inference_steps')
    minimum, maximum = checkpoint.get('min_period'), checkpoint.get('max_period')
    if (type(minimum) not in (int, float) or type(maximum) not in (int, float)
            or not 0 < minimum <= maximum or not math.isfinite(maximum)):
        raise ValueError('invalid sinusoidal time embedding periods')


def load_model_config(path, *, project_root):
    path = resolve_config_path(path, project_root)
    raw = read_json(path)
    if set(raw) - set(DEFAULTS):
        raise ValueError('unknown Pi05 model config section')
    config = copy.deepcopy(DEFAULTS)
    for section, values in raw.items():
        if not isinstance(values, dict) or set(values) - (set(DEFAULTS[section]) | EXTRA[section]):
            raise ValueError(f'unknown or invalid {section} settings; pi05_lift2 uses the dev export, '
                             'not native saved processors (use pi05 for those)')
        config[section].update(values)
    model = config['model']
    for key in ('checkpoint', 'tokenizer_path'):
        value = model.get(key)
        if not isinstance(value, str) or not value or '://' in value:
            raise ValueError(f'{key} must be an explicit local directory')
        model[key] = str(resolve_config_path(value, path.parent))
        if not Path(model[key]).is_dir():
            raise FileNotFoundError(f'{key} directory is missing: {model[key]}')
    checkpoint = read_json(local_file(model['checkpoint'], 'config.json'))
    local_file(model['checkpoint'], 'model.safetensors')
    _validate_checkpoint(checkpoint)
    _validate_training(model['checkpoint'])
    config['statistics'] = _load_statistics(model['checkpoint'])
    tokenizer = Path(model['tokenizer_path'])
    local_file(tokenizer, 'tokenizer_config.json')
    if not any((tokenizer / name).is_file() and (tokenizer / name).stat().st_size
               for name in ('tokenizer.json', 'tokenizer.model')):
        raise FileNotFoundError(f'tokenizer vocabulary missing: {tokenizer}')
    model['chunk_size'] = _positive_int(checkpoint.get('chunk_size'), 'chunk_size')
    model['max_action_dim'] = checkpoint['max_action_dim']
    execute = _positive_int(checkpoint.get('n_action_steps'), 'n_action_steps')
    if execute > model['chunk_size']:
        raise ValueError('n_action_steps cannot exceed chunk_size')
    if type(model['compile_model']) is not bool:
        raise ValueError('compile_model must be boolean')
    if type(model['warmup_runs']) is not int or model['warmup_runs'] < 0:
        raise ValueError('warmup_runs must be a nonnegative integer')
    if not isinstance(model['device'], str) or not re.fullmatch(r'cpu|cuda(?::\d+)?', model['device']):
        raise ValueError('device must be cpu, cuda, or cuda:N')
    instruction = config['runtime'].get('instruction')
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError('runtime.instruction must be nonempty')
    if config['actions'].get('representation') != 'delta_joint':
        raise ValueError('dev training uses delta joints and absolute grippers; requires delta_joint')
    if config['observations'].get('camera_map') != CAMERA_MAP:
        raise ValueError(f'dev camera_map must preserve head/left/right order: {CAMERA_MAP}')
    for key in ('state_scale', 'state_offset'):
        values = config['observations'][key]
        if (not isinstance(values, list) or len(values) != 14
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in values)
                or (key == 'state_scale' and any(v == 0 for v in values))):
            raise ValueError(f'{key} must contain 14 finite values; scale must be nonzero')
        with np.errstate(over='ignore', under='ignore', invalid='ignore'):
            converted = np.asarray(values, dtype=np.float32)
        if not np.isfinite(converted).all() or key == 'state_scale' and (converted == 0).any():
            raise ValueError(f'{key} cannot overflow or lose invertibility in float32')
    execution = config['execution']
    execution['chunk_size'] = execute
    if execution['request_when_remaining'] is None:
        execution['request_when_remaining'] = max(1, math.ceil(execute * .6))
    threshold = execution['request_when_remaining']
    if type(threshold) is not int or not 1 <= threshold <= execute:
        raise ValueError('request_when_remaining must be in [1, n_action_steps]')
    timeout = execution['inference_timeout_s']
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('inference_timeout_s must be positive and finite')
    config['checkpoint_config'] = checkpoint
    return config


def validate_manifest(config):
    params = config.get('params', {})
    unsupported = sorted(name for name in params if name.startswith('gripper_') and name not in GRIPPER_DEFAULTS)
    if unsupported:
        raise ValueError(f'unsupported gripper parameters: {", ".join(unsupported)}')
    execution = config.get('execution', {})
    # RC owns call scheduling; policies only constrain their action format.
    if execution.get('control_mode') != 'qpos':
        raise ValueError('Pi05 returns qpos actions')
    allowed = {'execution.mode', 'execution.call_every_actions', 'params.task',
               *('params.'+name for name in GRIPPER_DEFAULTS)}
    if any(path not in allowed for path in config.get('hot_reloadable', [])):
        raise ValueError('only RC scheduling, task and gripper rules are hot reloadable')
    for name, default in GRIPPER_DEFAULTS.items():
        value = params.get(name, default)
        if name == 'gripper_lower_threshold' and value is None:
            continue
        if name.endswith('_value'):
            parse_gripper_value(value, 'params.'+name)
        elif type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f'params.{name} must be a finite number')
    task = params.get('task')
    if task is not None and (not isinstance(task, str) or not task.strip()):
        raise ValueError('params.task must be null or nonempty text')
    path = config.get('params', {}).get('model_config_path')
    if not isinstance(path, str) or not path.strip():
        raise ValueError('params.model_config_path must be nonempty')
