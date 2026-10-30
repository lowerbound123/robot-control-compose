"""Cold settings for the independent failure detector."""
import copy
import math
from urllib.parse import urlsplit

from rc_policy_sdk.actions import CONTROL_MODES


DEFAULT_PROMPT = (
    '根据task以及按执行步数排列的当前和历史三路相机图像，判断机器人执行是否已经进入失败状态。'
    '只有明确证据表明当前执行已经偏离任务且不能正常继续达成目标时才判定失败。'
    '尚未完成、正常过渡、短暂遮挡或证据不足不等于失败。'
    '禁止任何解释、推理过程、Markdown或动作建议。'
)
DEFAULT_PARAMS = {
    'task': '', 'prompt': DEFAULT_PROMPT, 'check_every_steps': 60, 'history_size': 5,
    'image_max_side': 1024, 'jpeg_quality': 85,
    'decision_api': {'enabled': False, 'model': 'gpt-6-luna',
                     'base_url': 'https://api.openai.com/v1', 'api_key': '', 'timeout_s': 30.},
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
    decision = params['decision_api']
    if not isinstance(decision, dict) or set(decision) != set(DEFAULT_PARAMS['decision_api']):
        raise ValueError('decision_api must contain enabled, model, base_url, api_key and timeout_s')
    if type(decision['enabled']) is not bool:
        raise ValueError('decision_api.enabled must be boolean')
    for group in ('provider', 'decision_api'):
        _validate_backend(params[group], group, required_key=(decision['enabled'] == (group == 'decision_api')))
    return params


def selected_provider(params):
    enabled = params['decision_api']['enabled']
    settings = dict(params['decision_api'] if enabled else params['provider'])
    settings.pop('enabled', None)
    settings['type'] = 'openai_decisions' if enabled else 'deepseek'
    return settings


def _validate_backend(provider, group, *, required_key):
    if not isinstance(provider, dict) or set(provider) != set(DEFAULT_PARAMS[group]):
        raise ValueError(f'{group} has missing or unknown fields')
    for name in ('model', 'api_key'):
        if (not isinstance(provider[name], str)
                or ((name == 'model' or required_key) and not provider[name].strip())):
            raise ValueError(f'{group}.{name} must be a string and nonempty when required')
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
        raise ValueError(f'{group}.timeout_s must be positive and finite')
