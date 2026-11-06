"""Connector configuration checks, also usable by stdlib-only setup."""
import re


def validate_connectors(config):
    robot = config.get('robot')
    if isinstance(robot, dict) and 'backend' in robot:
        raise ValueError('robot.backend 已移除；请迁移到 connectors 与 robot.connector，并将设备参数移入 connector 配置')
    instances = config.get('connectors')
    if not isinstance(instances, dict) or len(instances) != 1:
        raise ValueError('第一版必须配置一个 connector instance')
    if not isinstance(config.get('connector_directory'), str) or not config['connector_directory']:
        raise ValueError('connector_directory is required')
    if not isinstance(robot, dict) or set(robot) != {'connector'} or robot['connector'] not in instances:
        raise ValueError('robot.connector must select a configured connector')
    for identity, value in instances.items():
        if not isinstance(identity, str) or not re.fullmatch(r'[a-z][a-z0-9_-]*', identity):
            raise ValueError('invalid connector instance id')
        if not isinstance(value, dict) or set(value) != {'source', 'config'}:
            raise ValueError('connector requires source and config')
        if not isinstance(value['source'], str) or not re.fullmatch(r'[a-z][a-z0-9_-]*', value['source']):
            raise ValueError('invalid connector source')
        if not isinstance(value['config'], str) or not value['config']:
            raise ValueError('connector config path is required')
