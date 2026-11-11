"""Connector configuration checks, also usable by stdlib-only setup."""
import re


IDENTITY = re.compile(r'[a-z][a-z0-9_-]*')
PROFILE = re.compile(r'[a-z0-9][a-z0-9_-]{0,47}')


def _identity(value, label):
    if not isinstance(value, str) or not IDENTITY.fullmatch(value):
        raise ValueError('invalid connector ' + label)


def _spec(value):
    if not isinstance(value, dict) or set(value) != {'source', 'config'}:
        raise ValueError('connector requires source and config')
    _identity(value['source'], 'source')
    if not isinstance(value['config'], str) or not value['config']:
        raise ValueError('connector config path is required')


def validate_connector_profile(value):
    """A profile selects one connector; device settings stay in its own file."""
    if not isinstance(value, dict) or set(value) != {'connector', 'source', 'config'}:
        raise ValueError('connector profile requires connector, source and config')
    _identity(value['connector'], 'instance id')
    _spec({key: value[key] for key in ('source', 'config')})


def validate_connectors(config):
    robot = config.get('robot')
    if isinstance(robot, dict) and 'backend' in robot:
        raise ValueError('robot.backend 已移除；请迁移到 robot.profile 或 connectors 与 robot.connector，并将设备参数移入 connector 配置')
    if not isinstance(config.get('connector_directory'), str) or not config['connector_directory']:
        raise ValueError('connector_directory is required')
    if 'robot_profiles_directory' in config and (not isinstance(config['robot_profiles_directory'], str) or not config['robot_profiles_directory']):
        raise ValueError('robot_profiles_directory must be a nonempty path')
    if isinstance(robot, dict) and 'profile' in robot:
        if set(robot) != {'profile'}:
            raise ValueError('robot.profile cannot be mixed with inline connection settings')
        if not isinstance(robot['profile'], str) or not PROFILE.fullmatch(robot['profile']):
            raise ValueError('invalid connector profile name')
        # An optional direct selector is dormant while a profile is selected.
        if 'connectors' not in config:
            return
    instances = config.get('connectors')
    if not isinstance(instances, dict) or len(instances) != 1:
        raise ValueError('第一版必须配置一个 connector instance')
    for identity, value in instances.items():
        _identity(identity, 'instance id')
        _spec(value)
    if isinstance(robot, dict) and set(robot) == {'profile'}:
        return
    if not isinstance(robot, dict) or set(robot) != {'connector'} or robot['connector'] not in instances:
        raise ValueError('robot.connector must select a configured connector')
