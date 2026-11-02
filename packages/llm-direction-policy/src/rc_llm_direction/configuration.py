"""Cold manifest settings and supported provider profiles for direction decisions."""
import copy
import math

from rc_llm_provider_config import load_provider_config


DEFAULT_PROMPT = (
    '根据当前头部、左腕、右腕三路图像和任务，选择左右手下一步动作。'
    '每只手只选一个动作，使用小步移动；无需动作时选择 noop。'
    '仅在图像明确表明任务已经完成时报告 done。'
)
DEFAULT_PARAMS = {
    'task': '', 'prompt': DEFAULT_PROMPT,
    'request_when_remaining': 0, 'chunk_size': 30,
    'translation_m': .05, 'rotation_rad': .1, 'gripper_open': -3.4, 'gripper_close': 0.,
    'image_max_side': 1024, 'jpeg_quality': 85,
    'provider_config_path': 'provider.yaml',
}


def _number(value, name, *, positive=False):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or (positive and value <= 0)):
        raise ValueError(f'{name} must be a finite' + (' positive number' if positive else ' number'))


def validate_config(config):
    if config.get('execution', {}).get('control_mode') != 'epos':
        raise ValueError('llm_direction requires control_mode epos')
    if config.get('hot_reloadable'):
        raise ValueError('llm_direction settings are cold; reload after editing')
    supplied = config.get('params')
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT_PARAMS):
        raise ValueError('unknown llm_direction params')
    params = copy.deepcopy(DEFAULT_PARAMS)
    params.update(copy.deepcopy(supplied))
    for name in ('task', 'prompt'):
        if not isinstance(params[name], str) or not params[name].strip():
            raise ValueError(f'{name} must be a nonempty string')
    for name, minimum, maximum in (('chunk_size', 1, 4096), ('request_when_remaining', 0, 4095),
                                    ('image_max_side', 1, 4096), ('jpeg_quality', 1, 95)):
        if type(params[name]) is not int or not minimum <= params[name] <= maximum:
            raise ValueError(f'{name} must be an integer from {minimum} to {maximum}')
    if params['request_when_remaining'] >= params['chunk_size']:
        raise ValueError('request_when_remaining must be smaller than chunk_size')
    for name in ('translation_m', 'rotation_rad', 'gripper_open', 'gripper_close'):
        _number(params[name], name, positive=name in ('translation_m', 'rotation_rad'))
    return params


def load_provider(path, policy_root):
    formats = {'deepseek': ('json_object',), 'openai': ('json_schema',),
               'openai_decisions': ('choice',), 'openai_compatible': ('json_schema', 'json_object', 'text')}
    return load_provider_config(path, policy_root, formats)
