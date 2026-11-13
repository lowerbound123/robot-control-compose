"""Optional, detached configuration projections; never performs file I/O."""
import copy
import json
from pathlib import PurePosixPath

from .configuration import same_config


def validate_export(value, config=None):
    if value is None:
        return None
    plain = json.loads(json.dumps(value, allow_nan=False))
    if not same_config(value, plain) or set(plain) != {'policy', 'settings', 'files'}:
        raise ValueError('config export requires plain policy/settings/files')
    if not isinstance(plain['policy'], dict) or not isinstance(plain['settings'], dict):
        raise ValueError('config export policy/settings must be mappings')
    if plain['policy'].keys() & plain['settings'].keys():
        raise ValueError('config export settings cannot shadow policy fields')
    if config is not None:
        projection = plain['policy']
        if set(projection) != set(config):
            raise ValueError('config export cannot change policy metadata')
        for key in config:
            if key != 'params' and not same_config(projection[key], config[key]):
                raise ValueError('config export cannot change policy metadata')
        if not isinstance(projection.get('params'), dict) or any(
                key not in config['params'] or not same_config(data, config['params'][key])
                for key, data in projection['params'].items()):
            raise ValueError('config export may only omit runtime params')
    files = plain['files']
    if not isinstance(files, list) or len(files) > 32:
        raise ValueError('config export files must be a bounded list')
    paths = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {'path', 'data'}:
            raise ValueError('config export file requires path/data')
        name = entry['path']
        if not isinstance(name, str) or not name or '\\' in name:
            raise ValueError('invalid config export path')
        path = PurePosixPath(name)
        if ('..' in path.parts or path.as_posix() != name
                or path.suffix != '.json' or name in paths or not isinstance(entry['data'], dict)):
            raise ValueError('config export requires unique JSON paths')
        paths.add(name)
    return plain


def export_config(module, context, config):
    callback = getattr(module, 'export_config', None)
    if not callable(callback):
        return None
    return validate_export(callback(context, copy.deepcopy(config)), config)
