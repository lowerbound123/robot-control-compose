"""Named connection settings; validation is safe in the setup interpreter."""
import copy
from pathlib import Path
import re

PROFILE = re.compile(r'[a-z0-9][a-z0-9_-]{0,47}')
CAMERAS = {'head', 'left_wrist', 'right_wrist'}


def profile_name(value):
    if not isinstance(value, str) or not PROFILE.fullmatch(value):
        raise ValueError('invalid connector profile name')
    return value


def validate_robot(config, *, selector=False):
    """Validate a named connector selection, never a device implementation."""
    if selector and isinstance(config, dict) and set(config) == {'profile'}:
        profile_name(config['profile'])
        return
    from .connector_config import validate_connector_profile
    validate_connector_profile(config)


class ConnectorProfiles:
    def __init__(self, directory):
        self.directory = Path(directory).absolute()

    def read(self, name):
        from .yaml_config import load_yaml_config
        name = profile_name(name)
        path = self.directory / (name + '.yaml')
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('symlink connector profiles are not supported')
        config = load_yaml_config(path)
        validate_robot(config)
        return copy.deepcopy(config)

    def list(self):
        rows = []
        for path in sorted(self.directory.glob('*.yaml')):
            try:
                config = self.read(path.stem)
                rows.append(dict(name=path.stem, source=config['source'],
                                 connector=config['connector'], config=config['config'], valid=True))
            except (ValueError, OSError) as exc:
                rows.append(dict(name=path.stem, valid=False, error=str(exc)))
        return rows
