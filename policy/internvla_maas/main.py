"""Remote InternVLA action chunks with Qwen single scheduling and processing."""
import copy
import shlex
import json
from pathlib import Path

from rc_internvla_maas.configuration import load_model_config, validate_manifest
from rc_internvla_maas.runtime import AsyncMaasPolicy

POLICY_ROOT = Path(__file__).resolve().parent


def initialize(config, *, runtime):
    validate_config(config)
    settings = load_model_config(config['params']['model_config_path'], project_root=POLICY_ROOT)
    return AsyncMaasPolicy(settings, project_root=POLICY_ROOT)


def policy(context, state, action_chunk, images, config, *, runtime):
    return context.step(state, action_chunk, images, runtime=runtime)


def monitor(context):
    return context.monitor()


def switch_model(context, config_path):
    return context.switch_model(config_path)


def validate_config(config):
    validate_manifest(config)


def reset_context(context):
    context.reset()


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
    allowed_params = {'model_config_path'}
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
    if key == 'params.model_config_path':
        load_model_config(candidate['params']['model_config_path'], project_root=POLICY_ROOT)
    effect = ('reinitialize' if key in {'params.model_config_path', 'execution.control_mode',
                                     'execution.tui_command_workers'}
              else 'reset' if key == 'params.task' else None)
    config.setdefault(section, {})[leaf] = candidate[section][leaf]
    if effect is not None:
        callback(effect)
