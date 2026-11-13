"""Instance-owned model settings and side-effect-free configuration proposals."""
import copy
import json

from .configuration import DEFAULTS, load_model_config, model_defaults, read_json, resolve_path

SNAPSHOT = '_model_config_snapshot'


def initial_settings(config, root, robot_type):
    path = resolve_path(config['params']['model_config_path'], root)
    raw = config['params'].get(SNAPSHOT)
    raw = model_defaults(read_json(path) if raw is None else json.loads(raw))
    effective = load_model_config(path, project_root=root, robot_type=robot_type, raw=raw)
    return raw, effective, path


def current_settings(context, config):
    raw = config['params'].get(SNAPSHOT)
    return copy.deepcopy(context.model_config_snapshot if raw is None else json.loads(raw))


def configure_model(context, config, key, value, callback):
    parts = key.split('.')
    if (len(parts) != 3 or parts[1] not in DEFAULTS or parts[2] not in DEFAULTS[parts[1]]):
        raise ValueError('unsupported configuration key: ' + key)
    raw = current_settings(context, config)
    raw[parts[1]][parts[2]] = value
    load_model_config(config['params']['model_config_path'], project_root=context.project_root,
                      robot_type=context.robot_type, raw=raw)
    action = ('reinitialize' if parts[1] in ('model', 'inference') else None
              if parts[1:] == ['execution', 'inference_timeout_s'] else 'reset')
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
