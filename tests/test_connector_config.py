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


def test_profile_selection_is_valid_without_a_direct_connector_table():
    from robot_control.connector_config import validate_connectors
    good = {'connector_directory': '../connector', 'robot': {'profile': 'lab-left'}}
    validate_connectors(good)
    for robot in ({'profile': '../lab'}, {'profile': 'lab', 'connector': 'body'}, {'profile': True}):
        with pytest.raises(ValueError):
            validate_connectors({**good, 'robot': robot})


@pytest.mark.parametrize('config_path', ['devices/main.yaml', '/tmp/device.yaml'])
def test_profile_device_path_and_source_remain_explicit(config_path):
    from robot_control.connector_config import validate_connector_profile
    profile = {'connector': 'body', 'source': 'custom_source', 'config': config_path}
    validate_connector_profile(profile)
    for extra in ({'backend': 'mock'}, {'frequency_hz': 10}, {'initial_qpos': [0] * 14}):
        with pytest.raises(ValueError):
            validate_connector_profile({**profile, **extra})
    with pytest.raises(ValueError):
        validate_connector_profile({**profile, 'source': '../mock'})
    with pytest.raises(ValueError):
        validate_connector_profile({**profile, 'config': ''})
