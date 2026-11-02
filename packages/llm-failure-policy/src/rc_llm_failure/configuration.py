"""Cold settings for the independent failure detector."""
import copy

from rc_llm_provider_config import load_provider_config
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
    'provider_config_path': 'provider.yaml',
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
    return params


def load_provider(path, policy_root):
    formats = {'deepseek': (), 'openai_decisions': ()}
    return load_provider_config(path, policy_root, formats)
