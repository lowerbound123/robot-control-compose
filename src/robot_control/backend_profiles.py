"""Named connection settings; validation is safe in the setup interpreter."""
import copy
import math
from pathlib import Path
import re

PROFILE = re.compile(r'[a-z0-9][a-z0-9_-]{0,47}')
CAMERAS = {'head', 'left_wrist', 'right_wrist'}


def profile_name(value):
    if not isinstance(value, str) or not PROFILE.fullmatch(value):
        raise ValueError('invalid backend profile name')
    return value


def validate_robot(config, *, selector=False):
    if not isinstance(config, dict):
        raise ValueError('robot must be an object')
    if selector and 'profile' in config:
        if set(config) != {'profile'}:
            raise ValueError('robot.profile cannot be mixed with inline connection settings')
        profile_name(config['profile'])
        return
    if config.get('backend') not in ('mock', 'acone') or 'profile' in config:
        raise ValueError('robot.backend must be mock or acone')
    if 'domain_id' in config and (type(config['domain_id']) is not int or config['domain_id'] < 0):
        raise ValueError('domain_id must be a nonnegative integer')
    for key in ('connect_timeout_s', 'close_timeout_s', 'feedback_timeout_s', 'camera_timeout_s'):
        value = config.get(key, 10 if key == 'connect_timeout_s' else 2)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'{key} must be positive and finite')
    for key in ('initial_qpos', 'initial_epos'):
        if key in config:
            value = config[key]
            if (not isinstance(value, list) or len(value) != 14 or
                    any(type(v) not in (int, float) or not math.isfinite(v) for v in value)):
                raise ValueError(f'{key} requires 14 finite numbers')
    if 'image_size' in config:
        value = config['image_size']
        if not isinstance(value, list) or len(value) != 2 or any(type(v) is not int or v <= 0 for v in value):
            raise ValueError('image_size requires two positive integers')
    if 'camera_topics' in config:
        topics = config['camera_topics']
        if not isinstance(topics, dict) or set(topics) != CAMERAS or any(not isinstance(v, str) or not v for v in topics.values()):
            raise ValueError('camera_topics requires head, left_wrist and right_wrist')
    if any(key in config for key in ('frequency_hz', 'safety', 'reset_qpos')):
        raise ValueError('backend profiles cannot change controller settings')


class BackendProfiles:
    def __init__(self, directory):
        self.directory = Path(directory).absolute()

    def read(self, name):
        from .yaml_config import load_yaml_config
        name = profile_name(name)
        path = self.directory / (name + '.yaml')
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('symlink backend profiles are not supported')
        config = load_yaml_config(path)
        validate_robot(config)
        return copy.deepcopy(config)

    def list(self):
        rows = []
        for path in sorted(self.directory.glob('*.yaml')):
            try:
                config = self.read(path.stem)
                rows.append(dict(name=path.stem, backend=config['backend'], valid=True))
            except (ValueError, OSError) as exc:
                rows.append(dict(name=path.stem, valid=False, error=str(exc)))
        return rows
