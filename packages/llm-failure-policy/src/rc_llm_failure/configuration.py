"""Cold settings for the independent failure detector."""
import copy
from pathlib import Path

from rc_llm_provider_config import load_provider_config
from rc_policy_sdk.actions import CONTROL_MODES
from .decisions import DECISION_FIELDS
from .prompts import validate_prompt_fields


DEFAULT_PARAMS = {
    'check_every_steps': 60, 'history_size': 5,
    'image_max_side': 1024, 'jpeg_quality': 85,
    'provider_config_path': 'provider.yaml',
}


def validate_config(config):
    if config.get('execution', {}).get('control_mode') not in CONTROL_MODES:
        raise ValueError('llm_failure requires control_mode qpos, epos or mixed')
    if any(path != 'execution.on_backend_switch' for path in config.get('hot_reloadable', [])):
        raise ValueError('llm_failure settings are cold; reload after editing')
    supplied = config.get('params')
    if isinstance(supplied, dict) and {'task', 'prompt'}.intersection(supplied):
        raise ValueError('llm_failure task and prompt moved to the provider profile; '
                         'run scripts/migrate_llm_failure_prompts.py')
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULT_PARAMS):
        raise ValueError('unknown llm_failure params')
    params = copy.deepcopy(DEFAULT_PARAMS)
    params.update(copy.deepcopy(supplied))
    for name, minimum, maximum in (('check_every_steps', 1, 10**9), ('history_size', 2, 16),
                                    ('image_max_side', 1, 4096), ('jpeg_quality', 1, 95)):
        if type(params[name]) is not int or not minimum <= params[name] <= maximum:
            raise ValueError(f'{name} must be an integer from {minimum} to {maximum}')
    return params


def load_provider(path, policy_root):
    fields = {'task', 'prompt', 'response_instructions', 'input_templates'}
    profile_fields = {'deepseek': fields, **{kind: fields | extra for kind, extra in DECISION_FIELDS.items()}}
    settings = load_provider_config(path, policy_root, dict.fromkeys(profile_fields, ()), extra_fields=profile_fields)
    validate_prompt_fields(settings, Path(policy_root) / path)
    return settings
