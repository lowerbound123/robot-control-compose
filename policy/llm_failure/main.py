"""Passthrough chain member; network checks run only in its isolated worker."""
import copy
import shlex
import json
from pathlib import Path

from rc_llm_failure.configuration import DEFAULT_PARAMS, load_provider, validate_config
from rc_llm_failure.runtime import FailurePolicy


POLICY_ROOT = Path(__file__).resolve().parent


def initialize(config):
    settings = load_provider(config['params'].get('provider_config_path'), POLICY_ROOT)
    return FailurePolicy(config, settings)


def switch_provider(context, config_path):
    load_provider(config_path, POLICY_ROOT)
    return {'params.provider_config_path': config_path}


def policy(context, state, action_chunk, images, config, *, runtime):
    return context.policy(action_chunk, images, runtime, state=state)


def monitor(context):
    return context.monitor()


def reset_context(context):
    context.reset_context()


def release(context):
    if context is not None:
        context.close()


def _configuration_value(raw):
    """This policy accepts JSON values and bare or single-quoted text."""
    try:
        return json.loads(raw)
    except ValueError:
        if raw.startswith("'"):
            words = shlex.split(raw)
            if len(words) != 1:
                raise ValueError("configuration value must be one quoted string")
            return words[0]
        if raw.startswith(('"', '[', '{')):
            raise ValueError("invalid JSON configuration value") from None
        return raw


def configure(context, config, key, value, callback):
    """Validate a detached candidate; the SDK applies lifecycle work after return."""
    allowed_params = set(DEFAULT_PARAMS)
    execution_fields = {'mode', 'call_every_actions', 'control_mode', 'tui_command_workers', 'on_connector_switch'}
    section, separator, leaf = key.partition('.')
    if (not separator or (section == 'params' and leaf not in allowed_params)
            or (section == 'execution' and leaf not in execution_fields)
            or section not in ('params', 'execution')):
        raise ValueError(f"unsupported configuration key: {key}")
    candidate = copy.deepcopy(config)
    candidate.setdefault(section, {})[leaf] = _configuration_value(value)
    execution = candidate['execution']
    if execution.get('on_connector_switch', 'reinitialize_policy') not in ('reset', 'reinitialize_policy'):
        raise ValueError('execution.on_connector_switch must be reset or reinitialize_policy')
    for field in ('call_every_actions', 'tui_command_workers'):
        count = execution.get(field, 4 if field == 'tui_command_workers' else 1)
        if type(count) is not int or count <= 0:
            raise ValueError(f'execution.{field} must be a positive integer')
    if execution.get('control_mode') not in ('qpos', 'epos', 'mixed'):
        raise ValueError('execution.control_mode must be qpos, epos or mixed')
    validate_config(candidate)
    if key == 'params.provider_config_path':
        load_provider(candidate['params']['provider_config_path'], POLICY_ROOT)
    effect = 'reinitialize' if section == 'params' or leaf in {'control_mode', 'tui_command_workers'} else None
    config.setdefault(section, {})[leaf] = candidate[section][leaf]
    if effect is not None:
        callback(effect)
