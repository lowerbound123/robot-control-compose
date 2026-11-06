"""A failed close must retain the actual reader/exit proof obligation."""
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from robot_control.connector import ConnectorRobot

ROOT = Path(__file__).resolve().parents[1]


def proxy(tmp_path):
    return ConnectorRobot({'connector_directory': str(ROOT / 'connector'),
        'connectors': {'body': {'source': 'mock', 'config': 'devices/default.yaml.example'}},
        'robot': {'connector': 'body'}}, tmp_path)


def test_close_retries_reader_join_instead_of_accepting_closed_flag(tmp_path):
    robot = proxy(tmp_path)
    finished = threading.Event()
    reader = threading.Thread(target=lambda: finished.wait(2), daemon=True)
    reader.start()
    robot._sampler = reader
    try:
        with pytest.raises(RuntimeError, match='reader did not stop'):
            robot.close(deadline=time.monotonic() + .03)
        assert not robot.closed
        with pytest.raises(RuntimeError, match='reader did not stop'):
            robot.close(deadline=time.monotonic() + .03)
        finished.set()
        reader.join(1)
        robot.close(deadline=time.monotonic() + 1)
        assert robot.closed
    finally:
        finished.set()
        reader.join(1)


def test_runtime_exit_is_rechecked_until_removal_is_proven(tmp_path):
    robot = proxy(tmp_path)
    class Runtime:
        removed = False
        attempts = 0
        def stop_and_remove(self, handle, deadline):
            self.attempts += 1
            if not self.removed:
                raise RuntimeError('container exit unconfirmed')
            return SimpleNamespace(forced=False)
    runtime = robot.runtime = Runtime()
    robot.handle = object()
    with pytest.raises(RuntimeError, match='exit unconfirmed'):
        robot.close()
    assert not robot.closed
    runtime.removed = True
    robot.close()
    assert runtime.attempts == 2 and robot.closed


def test_prepare_freezes_device_configuration_before_connection(tmp_path):
    device = tmp_path / 'device.yaml'
    device.write_text('initial_qpos: [1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]\n')
    robot = proxy(tmp_path)
    robot.config_path = device
    robot.prepare()
    device.write_text('initial_qpos: [2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2, 2]\n')
    robot.connect()
    try:
        assert robot.read_state()['qpos'] == [1.] * 14
    finally:
        robot.close()


def test_failed_container_creation_keeps_handle_for_exit_proof(tmp_path):
    import json
    from test_runtime_manager import manager
    runtime, engine = manager(tmp_path)
    runtime.context['connector_rpc_volume'] = 'connector-rpc'
    directory = tmp_path / 'connector/mock'
    directory.mkdir(parents=True)
    (directory / 'main.py').write_text('')
    (directory / 'compose.yaml').write_text(json.dumps({'services': {'connector': {'image': 'mock:local'}}}))
    device = directory / 'device.yaml'
    device.write_text('{}')
    robot = ConnectorRobot({'connector_directory': str(tmp_path / 'connector'),
        'connectors': {'body': {'source': 'mock', 'config': 'device.yaml'}},
        'robot': {'connector': 'body'}}, tmp_path)
    robot.runtime = runtime
    robot.prepare()
    assert not engine.containers
    engine.late = True
    with pytest.raises(TimeoutError):
        robot.connect()
    assert robot.handle is not None
    robot.close()
    assert not engine.containers


def test_same_connector_restart_has_distinct_deployment_generation(tmp_path):
    import json
    from test_runtime_manager import manager
    runtime, engine = manager(tmp_path)
    runtime.context['connector_rpc_volume'] = 'connector-rpc'
    directory = tmp_path / 'connector/mock'
    directory.mkdir(parents=True)
    (directory / 'compose.yaml').write_text(json.dumps({'services': {'connector': {'image': 'mock:local'}}}))
    first = runtime.start_connector('body', directory, {}, threading.Event(), time.monotonic() + 10)
    runtime.stop_and_remove(first, time.monotonic() + 10)
    second = runtime.start_connector('body', directory, {}, threading.Event(), time.monotonic() + 10)
    assert first.project != second.project and first.generation != second.generation
    runtime.stop_and_remove(second, time.monotonic() + 10)


def test_forced_exit_proof_is_terminal_but_reports_failure(tmp_path):
    import subprocess
    import sys
    robot = proxy(tmp_path)
    robot.process = subprocess.Popen([sys.executable, '-c',
        'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print("ready", flush=True); time.sleep(60)'],
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    assert robot.process.stdout.readline().strip() == 'ready'
    try:
        with pytest.raises(RuntimeError, match='forced'):
            robot.close(deadline=time.monotonic() + .15)
        assert robot.closed and robot.cleanup_forced
        # Manual recovery can consume the proven exit; the first failure remains
        # visible to the controller instead of retrying an already dead process.
        robot.close()
    finally:
        if robot.process.poll() is None: robot.process.kill()
        robot.process.wait()


def test_parent_exit_does_not_count_as_connector_process_tree_exit(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    robot = proxy(tmp_path)
    robot.process = subprocess.Popen([sys.executable, '-c',
        'import subprocess,sys; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); print(p.pid,flush=True)'],
        stdout=subprocess.PIPE, text=True, start_new_session=True)
    child = int(robot.process.stdout.readline())
    robot.process.wait(timeout=2)
    try:
        with pytest.raises(RuntimeError, match='process tree|forced'):
            robot.close(deadline=time.monotonic() + 1)
        assert robot.closed and robot.cleanup_forced
        with pytest.raises(ProcessLookupError): os.kill(child, 0)
    finally:
        try: os.killpg(robot.process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
