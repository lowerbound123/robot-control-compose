"""OpenWAM complete-chunk consumer for LIFT2."""
import json
import math
import shlex
from urllib.parse import urlsplit

from rc_openwam_maas_lift2.gripper import GRIPPER_PARAMS, gripper_rules
from rc_openwam_maas_lift2.runtime import OpenWAMPolicy

PARAMS = {'service_url', 'task', 'request_when_remaining', 'open_timeout_s', 'inference_timeout_s', *GRIPPER_PARAMS}
EXECUTION = {'control_mode', 'on_connector_switch', 'call_every_actions', 'mode', 'tui_command_workers'}


def validate_config(config):
    params = config['params']
    if set(params) != PARAMS:
        raise ValueError(f'OpenWAM parameter mismatch: {sorted(set(params) ^ PARAMS)}')
    if not isinstance(params['service_url'], str):
        raise ValueError('service_url must be a WebSocket URL')
    url = urlsplit(params['service_url'])
    if url.scheme not in ('ws', 'wss') or not url.hostname or url.port == 0 or url.fragment or url.username is not None:
        raise ValueError('service_url must be a ws:// or wss:// endpoint')
    if not isinstance(params['task'], str) or not params['task'].strip():
        raise ValueError('task must be nonempty text')
    threshold = params['request_when_remaining']
    if type(threshold) is not int or not 0 <= threshold <= 32:
        raise ValueError('request_when_remaining must be an integer in [0, 32]')
    for key in ('open_timeout_s', 'inference_timeout_s'):
        value = params[key]
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'{key} must be positive and finite')
    gripper_rules(params)
    execution = config['execution']
    if execution['control_mode'] != 'qpos' or execution['mode'] not in ('sync', 'async'):
        raise ValueError('OpenWAM requires qpos and sync or async scheduling')
    if execution.get('on_connector_switch', 'reinitialize_policy') not in ('reset', 'reinitialize_policy'):
        raise ValueError('invalid connector switch action')
    for key in ('call_every_actions', 'tui_command_workers'):
        value = execution.get(key, 1)
        if type(value) is not int or value <= 0:
            raise ValueError(f'{key} must be a positive integer')


def initialize(config, *, runtime):
    validate_config(config)
    return OpenWAMPolicy(config)


def policy(context, state, action_chunk, images, config, *, runtime):
    return context.step(state, action_chunk, images, runtime=runtime)


def monitor(context):
    return context.monitor()


def reset_context(context):
    context.reset()


def release(context):
    if context is not None:
        context.close()


def switch_task(context, task):
    if not isinstance(task, str) or not task.strip():
        raise ValueError('task must be nonempty text')
    return {'params.task': task}


def configure(context, config, key, value, callback):
    section, _, field = key.partition('.')
    if field not in {'params': PARAMS, 'execution': EXECUTION}.get(section, set()):
        raise ValueError(f'unsupported configuration key: {key}')
    try:
        parsed = json.loads(value)
    except ValueError:
        if value.startswith("'"):
            words = shlex.split(value)
            if len(words) != 1:
                raise ValueError('expected one quoted string')
            parsed = words[0]
        elif value.startswith(('"', '[', '{')):
            raise ValueError('invalid JSON configuration value') from None
        else:
            parsed = value
    candidate = {**config, section: {**config[section], field: parsed}}
    validate_config(candidate)
    config[section][field] = parsed
    if field in ('service_url', 'open_timeout_s', 'inference_timeout_s', 'tui_command_workers', 'control_mode'):
        callback('reinitialize')
    elif section == 'params':
        callback('reset')
