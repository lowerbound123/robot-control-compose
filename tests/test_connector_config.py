import pytest


def test_connector_configuration_is_explicit_and_single_instance():
    from robot_control.connector_config import validate_connectors
    good = {'connector_directory': '../connector', 'robot': {'connector': 'body'},
            'connectors': {'body': {'source': 'acone', 'config': 'devices/main.yaml'}}}
    validate_connectors(good)
    with pytest.raises(ValueError, match='robot.backend|迁移'):
        validate_connectors({'robot': {'backend': 'mock'}})
    with pytest.raises(ValueError, match='one|一个'):
        validate_connectors({**good, 'connectors': {**good['connectors'], 'second': good['connectors']['body']}})
    with pytest.raises(ValueError, match='connector'):
        validate_connectors({**good, 'robot': {'connector': 'missing'}})
