"""RC knows only the connector proxy; device implementations live outside RC."""
from ..connector import ConnectorRobot
from ..connector_config import validate_connectors


def make_robot(config, root='.'):
    validate_connectors(config)
    return ConnectorRobot(config, root)


__all__ = ['make_robot', 'ConnectorRobot']
