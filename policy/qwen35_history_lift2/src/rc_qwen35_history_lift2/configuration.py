"""Cold configuration and checkpoint preflight without importing Torch."""
import copy
import json
import math
from pathlib import Path

import numpy as np

PARTS = ('left_joint', 'left_gripper', 'right_joint', 'right_gripper')
WIDTHS = (6, 1, 6, 1)
LANGUAGE_FIELDS = ('Memory', 'Scene', 'Subtask')
DEFAULTS = {
    'model': {'checkpoint': '', 'vlm_model_name_or_path': 'Qwen/Qwen3.5-4B'},
    'runtime': {'instruction': ''},
    'execution': {'replan_every_actions': 25, 'pipelined': False, 'inference_timeout_s': 60.},
    'inference': {'num_inference_steps': None, 'use_cuda_graph': True, 'use_prefix_graph': True,
                  'generate_language': []},
    'history': {'enabled': True},
    'transition': {'trunk_mode': 'tail', 'trunk_smoothing': True, 'max_smoothing_steps': -1,
                   'exponential_factor': 5., 'exponential_smoothing': .25},
}


def resolve_path(value, root):
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError('configuration path must be nonempty')
    path = Path(value).expanduser()
    return (path if path.is_absolute() else Path(root) / path).resolve()


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f'duplicate JSON field {key}')
            result[key] = value
        return result
    result = json.loads(Path(path).read_text(), object_pairs_hook=pairs)
    json.dumps(result, allow_nan=False)
    if not isinstance(result, dict):
        raise ValueError(f'expected JSON object: {path}')
    return result


def positive(value, name, *, integer=False):
    if type(value) not in ((int,) if integer else (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be positive and finite')


def checkpoint_assets(directory, *, robot_type='arx_acone'):
    if robot_type not in ('arx_acone', 'arx_lift2'):
        raise ValueError(f'unsupported history robot_type: {robot_type}')
    directory = Path(directory)
    config = read_json(directory / 'config.json')
    if config.get('type') != 'qwen3_5_context_video_ki':
        raise ValueError('checkpoint type must be qwen3_5_context_video_ki')
    # The source sequence builder fixes the training horizon at 50.
    if type(config.get('chunk_size')) is not int or config['chunk_size'] != 50:
        raise ValueError('this source model requires checkpoint.chunk_size=50')
    for name in ('max_state_dim', 'max_action_dim'):
        if type(config.get(name)) is not int or config[name] != 32:
            raise ValueError(f'{name} must be 32 for the supported {robot_type} checkpoint')
    for name in ('context_slot_count', 'context_slot_stride', 'num_inference_steps'):
        positive(config.get(name), name, integer=True)
    positive(config.get('context_fps'), 'context_fps')
    if config.get('context_view_index', 0) != 0 or config.get('tokenize_state', False):
        raise ValueError('requires head-camera history and a continuous learned state')
    if config.get('max_num_views', 3) != 3:
        raise ValueError('requires three current camera views')
    if config.get('image_resize_mode') not in ('pad', 'long_side'):
        raise ValueError('unsupported image resize mode')
    if config.get('image_resize_mode') == 'long_side':
        positive(config.get('image_resize_long_side'), 'image_resize_long_side', integer=True)
    if config.get('context_resize_long_side') is not None:
        positive(config['context_resize_long_side'], 'context_resize_long_side', integer=True)
    dataset = read_json(directory / 'train_config.json').get('dataset', {})
    if dataset.get('action_mode') not in ('delta', 'abs'):
        raise ValueError('dataset.action_mode must be delta or abs')
    if dataset.get('norm_mode', 'mean_std') not in ('mean_std', 'q01_q99'):
        raise ValueError('unsupported state/action normalization')
    transforms = dataset.get('data_transforms', {}).get('inputs', [])
    if not transforms or transforms[-1].get('type') != 'qwen3_5_context_video_ki_processor':
        raise ValueError('train_config must end with the KI video processor')
    stats = read_json(directory / 'stats.json').get(robot_type)
    if not isinstance(stats, dict):
        raise ValueError(f'stats.json requires {robot_type}')
    mode = dataset.get('norm_mode', 'mean_std')
    fields = ('mean', 'std') if mode == 'mean_std' else ('q01', 'q99')
    composed = {}
    all_keys = {f'{prefix}.{part}.position' for prefix in ('states', 'actions') for part in PARTS}
    pad_keys = dataset.get('pad_keys') or []
    if not isinstance(pad_keys, list) or any(key not in all_keys for key in pad_keys):
        raise ValueError(f'unsupported pad_keys for {robot_type}')
    for prefix in ('states', 'actions'):
        values = {key: [] for key in fields}
        for part, width in zip(PARTS, WIDTHS):
            sub = stats.get(f'{prefix}.{part}.position', {})
            for key in fields:
                array = np.atleast_1d(np.asarray(sub.get(key), dtype=np.float32))
                if array.shape != (width,) or not np.isfinite(array).all():
                    raise ValueError(f'invalid {robot_type} {prefix}.{part}.{key} statistics')
                values[key].append(array)
        composed[prefix] = {key: np.concatenate(value) for key, value in values.items()}
        if mode == 'mean_std' and (composed[prefix]['std'] < 0).any():
            raise ValueError('standard deviations must be nonnegative')
        if mode == 'q01_q99' and (composed[prefix]['q99'] < composed[prefix]['q01']).any():
            raise ValueError('q99 must not be below q01')
    if not (directory / 'model.safetensors').is_file():
        raise FileNotFoundError('checkpoint requires model.safetensors')
    return config, dataset, composed


def model_defaults(raw):
    """Keep user path strings intact in the owned, saveable JSON snapshot."""
    config = copy.deepcopy(DEFAULTS)
    for section, values in raw.items():
        if section not in DEFAULTS or not isinstance(values, dict) or set(values) - set(DEFAULTS[section]):
            raise ValueError(f'unknown configuration fields in {section}')
        config[section].update(copy.deepcopy(values))
    return config


def load_model_config(path, *, project_root, robot_type='arx_acone', raw=None):
    path = resolve_path(path, project_root)
    config = model_defaults(read_json(path) if raw is None else raw)
    from rc_policy_sdk.transitions import TrajectoryAddTransition
    for name in ('exponential_factor', 'exponential_smoothing'):
        positive(config['transition'][name], 'transition.' + name)
    TrajectoryAddTransition(**config['transition'])
    language = config['inference']['generate_language']
    if (not isinstance(language, list)
            or any(type(field) is not str or field not in LANGUAGE_FIELDS for field in language)
            or len(language) != len(set(language))):
        raise ValueError('inference.generate_language must be a unique list of Memory, Scene and Subtask')
    config['inference']['generate_language'] = [field for field in LANGUAGE_FIELDS if field in language]
    config['model']['checkpoint'] = str(resolve_path(config['model']['checkpoint'], path.parent))
    base = config['model']['vlm_model_name_or_path']
    if not isinstance(base, str) or not base.strip():
        raise ValueError('vlm_model_name_or_path must be nonempty')
    if base.startswith(('.', '/', '~')):
        base = str(resolve_path(base, path.parent))
    config['model']['vlm_model_name_or_path'] = base
    checkpoint, _, _ = checkpoint_assets(config['model']['checkpoint'], robot_type=robot_type)
    x = config['execution']['replan_every_actions']
    if type(x) is not int or not 1 <= x <= checkpoint['chunk_size']:
        raise ValueError('replan_every_actions must be in [1, checkpoint.chunk_size]')
    positive(config['execution']['inference_timeout_s'], 'inference_timeout_s')
    for section, key in [('execution', 'pipelined'), ('history', 'enabled'),
                         ('inference', 'use_cuda_graph'), ('inference', 'use_prefix_graph')]:
        if type(config[section][key]) is not bool:
            raise ValueError(f'{section}.{key} must be boolean')
    steps = config['inference']['num_inference_steps']
    if steps is not None:
        positive(steps, 'num_inference_steps', integer=True)
    task = config['runtime']['instruction']
    if not isinstance(task, str) or not task.strip():
        raise ValueError('runtime.instruction must be nonempty')
    return config


def validate_manifest(config):
    execution = config.get('execution', {})
    if (execution.get('mode') != 'sync' or type(execution.get('call_every_actions')) is not int
            or execution['call_every_actions'] != 1 or execution.get('control_mode') != 'qpos'):
        raise ValueError('history requires sync callbacks every complete qpos action; use model.json pipelined')
    if any(path not in ('params.task', 'params.gripper_threshold', 'execution.on_connector_switch')
           for path in config.get('hot_reloadable', [])):
        raise ValueError('only params.task, params.gripper_threshold and execution.on_connector_switch are hot reloadable')
    params = config.get('params', {})
    if 'gripper_threshold' in params:
        threshold = params['gripper_threshold']
        if type(threshold) not in (int, float) or not math.isfinite(threshold):
            raise ValueError('params.gripper_threshold must be a finite number')
    if 'arm_clip_max_delta_rad' in params:
        positive(params['arm_clip_max_delta_rad'], 'params.arm_clip_max_delta_rad')
    task = config.get('params', {}).get('task')
    if task is not None and (not isinstance(task, str) or not task.strip()):
        raise ValueError('params.task must be null or nonempty text')
    resolve_path(config.get('params', {}).get('model_config_path'), '.')
