"""Real ROS Context replacement, isolated simulator domains and real example worker."""
import json
import os
import subprocess
import sys
import uuid

import pytest

from config_helpers import read_config
from test_backend_switch import profile
from test_example_controller import ROOT, command, example_rc
from test_ros_integration import _stop_simulator


def test_repeated_mock_ros_and_named_domain_switches_keep_policy_process(example_rc, monkeypatch):
    pytest.importorskip('rclpy', reason='requires ROS Humble runtime')
    pytest.importorskip('robot_control_test_msgs.msg', reason='requires compiled test messages')
    rc = example_rc
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    folder = rc.root / rc.config.get('robot_profiles_directory', 'robots')
    folder.mkdir(parents=True, exist_ok=True)
    processes, logs = [], []
    worker = rc._worker
    try:
        for index in range(2):
            namespace = '/robot_control_test/switch_' + uuid.uuid4().hex[:8]
            config = read_config(ROOT / 'configs/rc.ros-test.yaml.example')['robot']
            original = config['namespace'] + '/'
            config.update(namespace=namespace, domain_id=141+index, connect_timeout_s=8)
            for key in ('feedback_left_topic', 'feedback_right_topic', 'command_left_topic', 'command_right_topic'):
                config[key] = namespace + '/' + config[key].removeprefix(original)
            config['camera_topics'] = {name: namespace + '/' + topic.removeprefix(original)
                                      for name, topic in config['camera_topics'].items()}
            (folder / f'ros-{index}.yaml').write_text(json.dumps(config))
            log = (rc.root / f'ros-{index}.log').open('w')
            logs.append(log)
            processes.append(subprocess.Popen([sys.executable, '-m', 'robot_control_test_sim.simulator', '--namespace', namespace],
                env={**os.environ, 'ROS_DOMAIN_ID': str(141+index)}, stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
        profile(rc, 'mock-return', .1)
        command(rc, 'set example.execution.on_backend_switch "reset"')
        context = command(rc, 'example')['context_id']
        for name in ('ros-0', 'mock-return', 'ros-1', 'ros-0', 'mock-return'):
            old = rc.robot.raw
            command(rc, 'backend switch ' + name)
            assert rc._worker is worker and rc._worker.pid == worker.pid
            assert command(rc, 'example')['context_id'] == context
            assert rc.status()['backend']['profile'] == name and rc.status()['state'] == 'paused'
            assert len(rc.robot.read_images()) == 3
            if hasattr(old, '_spin_thread'):
                assert not old._spin_thread.is_alive() and not old._decoder_thread.is_alive()
            if name.startswith('ros-'):
                assert rc.robot.raw._context.get_domain_id() == 141 + int(name[-1])
        command(rc, 'set example.execution.on_backend_switch "reinitialize_policy"')
        command(rc, 'backend switch ros-1')
        assert rc._worker is worker and command(rc, 'example')['context_id'] != context
    finally:
        # Close the active connection before its simulator; avoid a stale-feedback fault during teardown.
        rc.submit('exit')
        assert rc.done.wait(15)
        for process in processes:
            _stop_simulator(process)
        for log in logs:
            log.close()
