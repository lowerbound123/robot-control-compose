import time
import pytest


def test_sources_are_independent_and_mock_reads_do_not_create_samples():
    from connector_helpers import MockRobot, acone, mock
    for module in (mock, acone):
        assert callable(module.create_connector)
        assert 'from robot_control' not in open(module.__file__).read()
    robot = MockRobot({'feedback_hz': 1})
    robot.connect()
    try:
        first = robot.read_state()
        second = robot.read_state()
        assert first['sequences'] == second['sequences']
        assert first['timestamp'] == second['timestamp']
    finally:
        robot.close()


@pytest.mark.parametrize('hz', [0, -1, float('nan'), True])
def test_mock_rejects_invalid_sampling_frequency_before_creating_threads(hz):
    from connector_helpers import MockRobot
    with pytest.raises(ValueError, match='feedback_hz'):
        MockRobot({'feedback_hz': hz})


def test_mock_command_inspection_history_is_bounded():
    from connector_helpers import MockRobot
    robot = MockRobot()
    robot.connect()
    try:
        for index in range(4100):
            robot.execute_arms({'left': {'mode': 'qpos', 'values': [float(index)] * 7}})
        assert len(robot.commands) == len(robot.command_modes) == 4096
        assert robot.commands[0][0] == 4 and robot.commands[-1][0] == 4099
    finally: robot.close()
