"""Schema 3 policy identity and dictionary validation; standard library only."""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import re

IDENTITY = re.compile(r'[a-z0-9][a-z0-9_-]{0,47}')
INSTANCE_IDENTITY = re.compile(r'[a-z0-9][a-z0-9_-]{0,63}')


def policy_id(value):
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise ValueError('policy ID must match [a-z0-9][a-z0-9_-]{0,47}')
    return value


def instance_id(value):
    if not isinstance(value, str) or not INSTANCE_IDENTITY.fullmatch(value):
        raise ValueError('instance ID must match [a-z0-9][a-z0-9_-]{0,63}')
    return value


def socket_path(identity):
    return f'/run/robot-control/{instance_id(identity)}.sock'


def validate_runtime_policy(config, source_id, runtime_id):
    """Validate a fixed host/source mapping without changing the live config."""
    policy_id(source_id)
    instance_id(runtime_id)
    if not isinstance(config, dict) or config.get('id') != runtime_id:
        raise ValueError('policy config ID differs from assigned runtime instance')
    validate_policy({**config, 'id': source_id}, source_id)


def validate_policy(config, identity=None):
    if not isinstance(config, dict) or config.get('schema_version') != 3:
        raise ValueError('policy schema_version must be 3')
    identity = policy_id(identity if identity is not None else config.get('id'))
    if config.get('id', identity) != identity:
        raise ValueError('policy id differs from directory name')
    if {'policy_host', 'conda_env', 'deployment'} & config.keys():
        raise ValueError('legacy policy_host/conda_env/deployment fields are not supported; use policy compose.yaml')
    if config.get('kind') == 'chain':
        if set(config) - {'schema_version', 'id', 'kind', 'policies'}:
            raise ValueError('invalid chain fields')
        members = config.get('policies')
        if not isinstance(members, list) or not members:
            raise ValueError('chain policies must be a nonempty list')
        for item in members:
            policy_id(item)
        return
    if config.get('kind', 'python') != 'python':
        raise ValueError('unknown policy kind')
    execution = config.get('execution')
    if not isinstance(execution, dict) or execution.get('mode') not in ('sync', 'async'):
        raise ValueError('invalid execution.mode')
    if type(execution.get('call_every_actions')) is not int or execution['call_every_actions'] <= 0:
        raise ValueError('call_every_actions must be a positive integer')
    if execution.get('control_mode') not in ('qpos', 'epos', 'mixed'):
        raise ValueError('invalid execution.control_mode')
    if not isinstance(config.get('params'), dict):
        raise ValueError('params must be an object')
    # Bootstrap loads this file directly without importing SDK __init__/NumPy.
    spec = importlib.util.spec_from_file_location('_catalog_commands', Path(__file__).with_name('commands.py'))
    commands = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(commands)
    commands.command_specs(config)
    paths = config.get('hot_reloadable', [])
    if not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
        raise ValueError('hot_reloadable must be field paths')
    for path in paths:
        if (path.split('.')[0] not in ('params', 'execution') or
                path in ('execution.control_mode', 'execution.tui_command_workers')):
            raise ValueError(f'{path}: process/identity/command fields cannot be hot reloadable')
        value = config
        for part in path.split('.'):
            if not isinstance(value, dict) or part not in value:
                raise ValueError('hot_reloadable path does not exist')
            value = value[part]
        if isinstance(value, dict):
            raise ValueError('hot_reloadable must name a leaf')
    timeouts = config.get('timeouts', {})
    if not isinstance(timeouts, dict) or set(timeouts) - {'initialize_s', 'inference_s', 'release_s'}:
        raise ValueError('invalid policy timeouts')
    for value in timeouts.values():
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError('policy timeouts must be finite and positive')
    json.dumps(config, allow_nan=False)
