"""Cold settings for the independent failure detector."""
import copy
import math
from urllib.parse import urlsplit

from rc_policy_sdk.actions import CONTROL_MODES


DEFAULT_PROMPT = (
    '根据task以及按执行步数排列的当前和历史三路相机图像，判断机器人执行是否已经进入失败状态。'
    '只有明确证据表明当前执行已经偏离任务且不能正常继续达成目标时才判定失败。'
    '尚未完成、正常过渡、短暂遮挡或证据不足不等于失败。'
    '只返回一个JSON对象，禁止任何解释、推理过程、Markdown或动作建议。'
)
DEFAULT_PARAMS = {
    'task': '', 'prompt': DEFAULT_PROMPT, 'check_every_steps': 60, 'history_size': 5,
    'image_max_side': 1024, 'jpeg_quality': 85,
    'provider': {'model': 'deepseek-flash', 'base_url': 'https://api.deepseek.com',
                 'api_key': '', 'timeout_s': 30.},
}


def validate_config(config):
    if config.get('execution', {}).get('control_mode') not in CONTROL_MODES:
        raise ValueError('llm_failure requires control_mode qpos, epos or mixed')
    if config.get('hot_reloadable'):
        raise ValueError('llm_failure settings are cold; reload after editing')
    supplied = config.get('params')
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT_PARAMS):
        raise ValueError('unknown llm_failure params')
    params = copy.deepcopy(DEFAULT_PARAMS)
    params.update(copy.deepcopy(supplied))
    for name in ('task', 'prompt'):
        if not isinstance(params[name], str) or not params[name].strip():
            raise ValueError(f'{name} must be a nonempty string')
    for name, minimum, maximum in (('check_every_steps', 1, 10**9), ('history_size', 2, 16),
                                    ('image_max_side', 1, 4096), ('jpeg_quality', 1, 95)):
        if type(params[name]) is not int or not minimum <= params[name] <= maximum:
            raise ValueError(f'{name} must be an integer from {minimum} to {maximum}')
    provider = params['provider']
    if not isinstance(provider, dict) or set(provider) != set(DEFAULT_PARAMS['provider']):
        raise ValueError('provider must contain model, base_url, api_key and timeout_s')
    for name in ('model', 'api_key'):
        if not isinstance(provider[name], str) or not provider[name].strip():
            raise ValueError(f'provider.{name} must be nonempty')
    try:
        url = urlsplit(provider['base_url'])
        valid = (url.scheme in ('http', 'https') and bool(url.hostname) and not url.username
                 and not url.password and not url.query and not url.fragment)
        url.port
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise ValueError('base_url must be HTTP(S) without credentials, query or fragment')
    timeout = provider['timeout_s']
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('provider.timeout_s must be positive and finite')
    return params
