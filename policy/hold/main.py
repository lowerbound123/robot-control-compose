"""Return an absolute qpos chunk that holds the measured pose."""
import copy
import shlex
import json

import numpy as np


def initialize(config):
    return {"config": config}


def policy(context, state, action_chunk, images, config):
    # The worker updates this same config object in place on accepted hot patches.
    chunk_size = context["config"]["params"]["chunk_size"]
    qpos = np.asarray(state["qpos"], dtype=np.float64).reshape(14)
    return np.repeat(qpos[None, :], chunk_size, axis=0)


def reset_context(context):
    pass


def release(context):
    pass


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
    allowed_params = {'chunk_size'}
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
    if execution['control_mode'] != 'qpos':
        raise ValueError('hold requires execution.control_mode qpos')
    size = candidate['params']['chunk_size']
    if type(size) is not int or size <= 0:
        raise ValueError('params.chunk_size must be a positive integer')

    effect = 'reinitialize' if key in {'execution.control_mode', 'execution.tui_command_workers'} else None
    config.setdefault(section, {})[leaf] = candidate[section][leaf]
    if effect is not None:
        callback(effect)
