import json
from pathlib import Path
import time

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_proxy_uses_child_process_and_local_immutable_caches(tmp_path):
    from robot_control.connector import ConnectorRobot
    config_file = tmp_path / 'device.yaml'
    config_file.write_text('{}')
    config = {'frequency_hz': 100, 'connector_directory': str(ROOT / 'connector'),
              'connectors': {'body': {'source': 'mock', 'config': str(config_file)}},
              'robot': {'connector': 'body'}}
    robot = ConnectorRobot(config, tmp_path)
    robot.connect()
    try:
        assert robot.process.pid != __import__('os').getpid()
        frame = robot.read_images()['head'][0]
        assert not frame.flags.writeable
        identity = robot.execute(np.ones(14))
        deadline = time.monotonic() + 2
        received = []
        while not received:
            received = robot.poll_results()
            assert time.monotonic() < deadline
            time.sleep(.005)
        assert received[0]['id'] == identity and received[0]['success']
        assert frame[0, 0].tolist() == [180, 0, 0]
        assert robot.recording_snapshot()['frames']['head']['image'].flags.writeable is False
    finally:
        robot.close()
    assert robot.process.poll() == 0


def test_first_image_read_after_idle_requests_refresh_without_sensor_fault(tmp_path):
    from robot_control.connector import ConnectorRobot, ImagesPending
    device = tmp_path / 'device.yaml'
    device.write_text('{"camera_timeout_s": 0.08}')
    robot = ConnectorRobot({'frequency_hz': 100, 'connector_directory': str(ROOT / 'connector'),
        'connectors': {'body': {'source': 'mock', 'config': str(device)}}, 'robot': {'connector': 'body'}}, tmp_path)
    robot.connect()
    try:
        with robot._lock:
            robot._frames.clear()  # Initial feedback can arrive before the first camera frame.
        with pytest.raises(ImagesPending): robot.read_images()
        robot.wait_images()
        time.sleep(.15)
        with pytest.raises(ImagesPending): robot.read_images()
        deadline = time.monotonic() + 1
        while True:
            try:
                assert len(robot.read_images()) == 3
                break
            except ImagesPending:
                assert time.monotonic() < deadline
                time.sleep(.005)
    finally: robot.close()


def test_forced_local_cleanup_uses_the_original_absolute_deadline(tmp_path):
    import subprocess
    import sys
    from robot_control.connector import ConnectorRobot
    robot = ConnectorRobot({'connector_directory': str(ROOT / 'connector'),
        'connectors': {'body': {'source': 'mock', 'config': 'devices/default.yaml.example'}},
        'robot': {'connector': 'body'}}, tmp_path)
    robot.process = subprocess.Popen([sys.executable, '-c',
        'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready",flush=True); time.sleep(60)'],
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    try:
        assert robot.process.stdout.readline().strip() == 'ready'
        started = time.monotonic()
        with pytest.raises(RuntimeError, match='forced'):
            robot.close(deadline=started + .15)
        assert time.monotonic() - started < .35
        assert robot.process.poll() is not None
    finally:
        if robot.process.poll() is None: robot.process.kill()
        robot.process.wait()


@pytest.mark.parametrize('field,value', [('connect_timeout_s', float('nan')), ('feedback_timeout_s', 0), ('camera_timeout_s', -1)])
def test_invalid_device_timeouts_fail_before_starting_a_process(tmp_path, field, value):
    from robot_control.connector import ConnectorRobot
    device = tmp_path / 'device.yaml'
    device.write_text(json.dumps({field: value}))
    robot = ConnectorRobot({'connector_directory': str(ROOT / 'connector'),
        'connectors': {'body': {'source': 'mock', 'config': str(device)}}, 'robot': {'connector': 'body'}}, tmp_path)
    try:
        with pytest.raises(ValueError): robot.connect()
        assert robot.process is None
    finally:
        if robot.process is not None and not robot.closed: robot.close()
