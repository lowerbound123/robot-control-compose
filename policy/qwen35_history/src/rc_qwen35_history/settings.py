"""Instance-owned model settings and side-effect-free configuration proposals."""
import copy
import json
import shlex

from .configuration import DEFAULTS, GRIPPER_DEFAULTS, validate_manifest, load_model_config, model_defaults, read_json, resolve_path

SNAPSHOT = '_model_config_snapshot'


def initial_settings(config, root, robot_type, load_config):
    path = resolve_path(config['params']['model_config_path'], root)
    raw = config['params'].get(SNAPSHOT)
    raw = model_defaults(read_json(path) if raw is None else json.loads(raw))
    effective = load_config(path, project_root=root, robot_type=robot_type, raw=raw)
    return raw, effective, path


def current_settings(context, config):
    raw = config['params'].get(SNAPSHOT)
    return copy.deepcopy(context.model_config_snapshot if raw is None else json.loads(raw))


def configure_model(context, config, key, value, callback, *, load_config, hot_model_keys):
    parts = key.split('.')
    if (len(parts) != 3 or parts[1] not in DEFAULTS or parts[2] not in DEFAULTS[parts[1]]):
        raise ValueError('unsupported configuration key: ' + key)
    raw = current_settings(context, config)
    raw[parts[1]][parts[2]] = value
    load_config(config['params']['model_config_path'], project_root=context.project_root,
                robot_type=context.robot_type, raw=raw)
    if parts[1] in ('model', 'inference'):
        action = 'reinitialize'
    elif key == 'model_config.execution.inference_timeout_s' or key in hot_model_keys:
        action = None
    else:
        action = 'reset'
    config['params'][SNAPSHOT] = json.dumps(raw, ensure_ascii=False, allow_nan=False)
    if action: callback(action)


def export_settings(context, config):
    raw = current_settings(context, config)
    projection = copy.deepcopy(config)
    projection['params'].pop(SNAPSHOT, None)
    path = context.model_config_file
    try:
        name = path.relative_to(context.project_root).as_posix()
    except ValueError:
        # Local hosts may run a source copy while the selected file remains in
        # the RC source directory. RC validates this absolute destination there.
        name = str(path)
    return {'policy': projection,
            'settings': {'model_config': raw,
                         'effective_task': config['params'].get('task') or raw['runtime']['instruction'],
                         'task_source': 'params.task' if config['params'].get('task') is not None else 'model_config.runtime.instruction'},
            'files': [{'path': name, 'data': raw}]}


def initialize_policy(config, root, policy_class, *, load_config=load_model_config):
    validate_manifest(config)
    raw, model, model_file = initial_settings(config, root, 'arx_acone', load_config)
    context = policy_class(model, policy_config=config, project_root=root)
    context.model_config_snapshot = raw
    context.model_config_file = model_file
    return context


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


def configure_policy(context, config, key, value, callback, root, *,
                     load_config=load_model_config, hot_model_keys=()):
    """Validate a detached candidate; the SDK applies lifecycle work after return."""
    if key.startswith('model_config.'):
        return configure_model(context, config, key, _configuration_value(value), callback,
                               load_config=load_config, hot_model_keys=hot_model_keys)
    allowed_params = {'model_config_path', 'task', *GRIPPER_DEFAULTS}
    execution_fields = {'mode', 'call_every_actions', 'control_mode', 'tui_command_workers'}
    section, separator, leaf = key.partition('.')
    if (not separator or (section == 'params' and leaf not in allowed_params)
            or (section == 'execution' and leaf not in execution_fields)
            or section not in ('params', 'execution')):
        raise ValueError(f"unsupported configuration key: {key}")
    candidate = copy.deepcopy(config)
    candidate.setdefault(section, {})[leaf] = _configuration_value(value)
    execution = candidate['execution']
    for field in ('call_every_actions', 'tui_command_workers'):
        count = execution.get(field, 4 if field == 'tui_command_workers' else 1)
        if type(count) is not int or count <= 0:
            raise ValueError(f'execution.{field} must be a positive integer')
    if execution.get('control_mode') not in ('qpos', 'epos', 'mixed'):
        raise ValueError('execution.control_mode must be qpos, epos or mixed')
    validate_manifest(candidate)
    if key == 'params.model_config_path':
        load_config(candidate['params']['model_config_path'], project_root=root)
        candidate['params'][SNAPSHOT] = None
    effect = ('reinitialize' if key in {'params.model_config_path', 'execution.control_mode',
                                     'execution.tui_command_workers'}
              else 'reset' if key == 'params.task' else None)
    config[section] = candidate[section]
    if effect is not None:
        callback(effect)
