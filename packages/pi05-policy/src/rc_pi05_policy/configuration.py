"""Local checkpoint preflight without importing Torch or LeRobot."""
import copy
import json
import math
from pathlib import Path
import re

import numpy as np

CAMERAS = ('head', 'left_wrist', 'right_wrist')
DEFAULTS = {
    'model': {'device': 'cuda', 'compile_model': False, 'warmup_runs': 1,
              'preprocessor_filename': 'policy_preprocessor.json',
              'postprocessor_filename': 'policy_postprocessor.json'},
    'runtime': {},
    'observations': {'state_scale': [1.0] * 14, 'state_offset': [0.0] * 14},
    'actions': {},
    'execution': {'request_when_remaining': None, 'inference_timeout_s': 60.0},
}
EXTRA = {'model': {'checkpoint', 'tokenizer_path'}, 'runtime': {'instruction'},
         'observations': {'camera_map'}, 'actions': {'representation'}, 'execution': set()}
STEP_CLASSES = {
    'NormalizerProcessorStep': 'normalizer_processor',
    'UnnormalizerProcessorStep': 'unnormalizer_processor',
    'TokenizerProcessorStep': 'tokenizer_processor',
    'DeviceProcessorStep': 'device_processor',
    'RenameObservationsProcessorStep': 'rename_observations_processor',
    'RelativeActionsProcessorStep': 'relative_actions_processor',
    'AbsoluteActionsProcessorStep': 'absolute_actions_processor',
}


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


def processor_step_key(step):
    """Return both LeRobot's override key and a stable built-in step identity."""
    key = step.get('registry_name')
    if key is None:
        key = step.get('class', '').rsplit('.', 1)[-1]
    return key, STEP_CLASSES.get(key, key)


def read_processor(checkpoint, filename):
    data = read_json(local_file(checkpoint, filename))
    steps = data.get('steps')
    if not isinstance(steps, list) or not steps:
        raise ValueError(f'{filename}: saved processor steps are required')
    for step in steps:
        if (not isinstance(step, dict) or not processor_step_key(step)[0]
                or not isinstance(step.get('config', {}), dict)):
            raise ValueError(f'{filename}: invalid processor step')
        if 'state_file' in step:
            local_file(checkpoint, step['state_file'])
    return data


def _positive_int(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(f'{name} must be a positive integer')
    return value


def _validate_features(checkpoint, observations):
    inputs, outputs = checkpoint.get('input_features', {}), checkpoint.get('output_features', {})
    for features, key, kind in ((inputs, 'observation.state', 'STATE'), (outputs, 'action', 'ACTION')):
        value = features.get(key, {})
        if value.get('type') != kind or value.get('shape') != [14]:
            raise ValueError(f'checkpoint {key} must declare exactly 14 {kind} values')
    mapping = observations.get('camera_map')
    if (not isinstance(mapping, dict) or set(mapping) != set(CAMERAS)
            or any(not isinstance(v, str) or not v for v in mapping.values())
            or len(set(mapping.values())) != 3):
        raise ValueError('camera_map must explicitly map head/left_wrist/right_wrist to three distinct features')
    visual = {k: v for k, v in inputs.items() if v.get('type') == 'VISUAL'}
    if set(mapping.values()) != set(visual):
        raise ValueError(f'camera_map must match checkpoint image features: {sorted(visual)}')
    for name, feature in visual.items():
        shape = feature.get('shape')
        if (not isinstance(shape, list) or len(shape) != 3 or shape[0] != 3
                or any(type(n) is not int or n <= 0 for n in shape)):
            raise ValueError(f'camera {name} must declare a CHW RGB feature')


def _validate_processors(config, checkpoint, pre, post):
    def selected(data, kind):
        return [s for s in data['steps'] if processor_step_key(s)[1] == kind]

    if len(selected(pre, 'tokenizer_processor')) != 1:
        raise ValueError('preprocessor must contain exactly one tokenizer_processor')
    for data, name in ((pre, 'normalizer_processor'), (post, 'unnormalizer_processor')):
        found = selected(data, name)
        if len(found) != 1 or not found[0].get('state_file'):
            raise ValueError(f'{name} requires saved normalization state; export the complete processor')
    relative = selected(pre, 'relative_actions_processor')
    absolute = selected(post, 'absolute_actions_processor')
    relative_enabled = any(s.get('config', {}).get('enabled', False) for s in relative)
    absolute_enabled = any(s.get('config', {}).get('enabled', False) for s in absolute)
    if relative_enabled != absolute_enabled or (checkpoint.get('use_relative_actions', False)
                                                and not absolute_enabled):
        raise ValueError('saved relative/absolute processors must be enabled as a matching pair')
    if absolute_enabled and config['actions']['representation'] != 'absolute':
        raise ValueError('absolute processor already converts relative actions; do not apply delta_joint again')
    for step in relative:
        settings = step.get('config', {})
        if settings.get('enabled') and settings.get('exclude_joints'):
            names = settings.get('action_names')
            if (not isinstance(names, list) or len(names) < 14
                    or any(not isinstance(name, str) or not name for name in names)):
                raise ValueError('relative action_names are required to preserve gripper exclusions')
    for step in selected(pre, 'rename_observations_processor'):
        # The adapter already emits the checkpoint's names. Renaming those names
        # elsewhere would make visual inputs silently absent downstream.
        renames = step.get('config', {}).get('rename_map', {})
        required = {'observation.state', *config['observations']['camera_map'].values()}
        if any(k in required and k != v for k, v in renames.items()):
            raise ValueError('saved camera/state rename_map conflicts with checkpoint feature names')


def load_model_config(path, *, project_root):
    path = resolve_config_path(path, project_root)
    raw = read_json(path)
    if set(raw) - set(DEFAULTS):
        raise ValueError('unknown Pi05 model config section')
    config = copy.deepcopy(DEFAULTS)
    for section, values in raw.items():
        if not isinstance(values, dict) or set(values) - (set(DEFAULTS[section]) | EXTRA[section]):
            raise ValueError(f'unknown or invalid {section} settings')
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
    if checkpoint.get('type') != 'pi05':
        raise ValueError('checkpoint type must be pi05 (LeRobot format)')
    local_file(model['checkpoint'], 'model.safetensors')
    tokenizer = Path(model['tokenizer_path'])
    local_file(tokenizer, 'tokenizer_config.json')
    if not any((tokenizer / name).is_file() for name in ('tokenizer.json', 'tokenizer.model', 'spiece.model')):
        raise FileNotFoundError(f'tokenizer vocabulary missing: {tokenizer}')
    model['chunk_size'] = _positive_int(checkpoint.get('chunk_size'), 'chunk_size')
    execute = _positive_int(checkpoint.get('n_action_steps'), 'n_action_steps')
    if execute > model['chunk_size']:
        raise ValueError('n_action_steps cannot exceed chunk_size')
    for key in ('max_state_dim', 'max_action_dim'):
        if _positive_int(checkpoint.get(key, 32), key) < 14:
            raise ValueError(f'{key} must accommodate all 14 ACOne dimensions')
    if type(checkpoint.get('n_obs_steps', 1)) is not int or checkpoint.get('n_obs_steps', 1) != 1:
        raise ValueError('Pi05 supports n_obs_steps=1 only')
    if checkpoint.get('rtc_config') is not None:
        raise ValueError('LeRobot RTC is unsupported; chunk scheduling is owned by RC')
    if checkpoint.get('use_visual_memory') or checkpoint.get('use_proprioceptive_memory'):
        raise ValueError('history/memory checkpoints are unsupported')
    if checkpoint.get('empty_cameras', 0):
        raise ValueError('Pi05 ACOne requires three real camera features, no empty_cameras')
    if type(model['compile_model']) is not bool:
        raise ValueError('compile_model must be boolean')
    if type(model['warmup_runs']) is not int or model['warmup_runs'] < 0:
        raise ValueError('warmup_runs must be a nonnegative integer')
    if not isinstance(model['device'], str) or not re.fullmatch(r'cpu|cuda(?::\d+)?', model['device']):
        raise ValueError('device must be cpu, cuda, or cuda:N')
    instruction = config['runtime'].get('instruction')
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError('runtime.instruction must be nonempty')
    if config['actions'].get('representation') not in ('absolute', 'delta_joint'):
        raise ValueError('actions.representation must explicitly be absolute or delta_joint')
    _validate_features(checkpoint, config['observations'])
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
    pre = read_processor(model['checkpoint'], model['preprocessor_filename'])
    post = read_processor(model['checkpoint'], model['postprocessor_filename'])
    _validate_processors(config, checkpoint, pre, post)
    return config


def validate_manifest(config):
    execution = config.get('execution', {})
    if (execution.get('mode') != 'sync' or execution.get('call_every_actions') != 1
            or execution.get('control_mode') != 'qpos'):
        raise ValueError('Pi05 requires sync callbacks every complete qpos action')
    if config.get('hot_reloadable'):
        raise ValueError('Pi05 configuration is cold; use switch-model')
    path = config.get('params', {}).get('model_config_path')
    if not isinstance(path, str) or not path.strip():
        raise ValueError('params.model_config_path must be nonempty')
