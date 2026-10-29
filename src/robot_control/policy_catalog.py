"""RC/setup policy discovery; never import a policy's implementation."""
import copy
import hashlib
from pathlib import Path

from rc_policy_sdk.catalog import policy_id, socket_path, validate_policy
from .config_paths import policy_config_path
from .yaml_config import load_yaml_config


def read_descriptor(directory):
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError('symlink policy directories are not supported')
    directory = directory.resolve()
    identity = policy_id(directory.name)
    path = policy_config_path(directory)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f'{identity}: missing or unreadable policy.yaml') from exc
    config = load_yaml_config(path)
    try:
        validate_policy(config, identity)
    except (ValueError, TypeError, KeyError):
        raise ValueError(f'{path}: invalid policy schema') from None
    config = copy.deepcopy(config)
    config['id'] = identity
    if config.get('kind') != 'chain':
        for name in ('main.py', 'compose.yaml'):
            if not (directory / name).is_file():
                raise ValueError(f'{identity}: missing {name}')
    return dict(id=identity, directory=str(directory), config=config,
                digest=hashlib.sha256(raw).hexdigest(), socket=socket_path(identity))


def discover(root):
    root = Path(root)
    rows = []
    if not root.is_dir():
        return rows
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or not any((folder / name).is_file() for name in ('policy.yaml', 'policy.yaml.example', 'policy.yml', 'policy.yml.example', 'policy.json', 'policy.json.example')):
            continue
        row = dict(id=folder.name, directory=str(folder), valid=False)
        try:
            policy_id(folder.name)
            path = policy_config_path(folder)
            if not path.is_file():
                row.update(state='needs_config', error='policy.yaml has not been configured')
            else:
                descriptor = read_descriptor(folder)
                config = descriptor['config']
                if config.get('kind') == 'chain':
                    for member in dict.fromkeys(config['policies']):
                        child = read_descriptor(root / member)
                        if child['config'].get('kind') == 'chain':
                            raise ValueError('nested native chains are not supported')
                row.update(valid=True, state='discovered', kind=config.get('kind', 'python'),
                           control_mode=config.get('execution', {}).get('control_mode'))
        except (ValueError, OSError, TypeError) as exc:
            row.update(state='invalid', error=str(exc))
        rows.append(row)
    return rows
