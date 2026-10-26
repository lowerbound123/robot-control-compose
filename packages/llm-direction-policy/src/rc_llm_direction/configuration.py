"""Cold policy settings. Credentials are environment-variable names only."""
import copy
import math
import re
from urllib.parse import urlsplit


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
    'provider': {'type': 'deepseek', 'model': 'deepseek-flash',
                 'base_url': 'https://api.deepseek.com', 'api_key_env': 'DEEPSEEK_API_KEY',
                 'output_format': 'json_object', 'timeout_s': 30.},
}
PROVIDERS = ('deepseek', 'openai_compatible', 'openai_decisions', 'openai')


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
    provider = params['provider']
    if not isinstance(provider, dict) or set(provider) != set(DEFAULT_PARAMS['provider']):
        raise ValueError('provider must contain type, model, base_url, api_key_env, output_format, timeout_s')
    if provider['type'] not in PROVIDERS:
        raise ValueError('unknown LLM provider type')
    if not isinstance(provider['model'], str) or not provider['model'].strip():
        raise ValueError('provider.model must be nonempty')
    if (not isinstance(provider['api_key_env'], str)
            or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', provider['api_key_env'])):
        raise ValueError('api_key_env must name an environment variable, never a literal key')
    if provider['api_key_env'].startswith(('PYTHON', 'CONDA', 'ROS_', 'RMW_', 'AMENT_', 'COLCON_', 'LD_', 'DYLD_')):
        raise ValueError('api_key_env uses a reserved interpreter or robotics environment name')
    try:
        url = urlsplit(provider['base_url'])
        valid_url = (url.scheme in ('http', 'https') and bool(url.hostname)
                     and not url.username and not url.password and not url.query and not url.fragment)
        url.port
    except (TypeError, ValueError, AttributeError):
        valid_url = False
    if not valid_url:
        raise ValueError('base_url must be an HTTP(S) base without credentials, query or fragment')
    allowed = {'deepseek': ('json_object',), 'openai': ('json_schema',),
               'openai_decisions': ('choice',),
               'openai_compatible': ('json_schema', 'json_object', 'text')}
    if provider['output_format'] not in allowed[provider['type']]:
        raise ValueError('output_format is unsupported by the selected provider adapter')
    _number(provider['timeout_s'], 'provider.timeout_s', positive=True)
    return params
