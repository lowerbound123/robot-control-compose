"""Real RC, UDS host, production Qwen entry and scheduler; CUDA is substituted."""
from project_templates import template_path, copy_templates
import json
import os
import time
from pathlib import Path

import pytest

from robot_control.controller import RobotController
from robot_control.robots import MockRobot
from test_host_transport import Host
from qwen_helpers import ROOT, events, prepare_policies, set_controls, wait


class ThreeCameraRobot(MockRobot):
    def read_images(self):
        import numpy as np
        return {name: (np.full((8, 8, 3), i, np.uint8), time.monotonic())
                for i, name in enumerate(('head', 'left_wrist', 'right_wrist'), 1)}


@pytest.fixture
def qwen_controller(tmp_path, monkeypatch):
    package = str(ROOT / 'packages/qwen35-policy/src')
    monkeypatch.setenv('PYTHONPATH', package + os.pathsep + os.environ.get('PYTHONPATH', ''))
    root = prepare_policies(tmp_path / 'policy')
    host = Host(root, 'qwen35_single')
    rc = json.loads((template_path(ROOT / 'configs/rc.mock.json')).read_text())
    rc['safety']['enabled'] = False  # Deterministic test actions encode the policy identity.
    rc.update(policy_directory=str(root), startup_policy='qwen35_single',
              outputs_directory=str(tmp_path / 'outputs'), frequency_hz=100,
              timeouts={'initialize_s': 5, 'inference_s': 3, 'release_s': 1},
              host_socket=host.socket)
    path = tmp_path / 'rc.json'
    path.write_text(json.dumps(rc))
    from host_helpers import LocalPolicyProcess
    controller = RobotController(path, robot=ThreeCameraRobot(), process_factory=LocalPolicyProcess)
    controller.start()
    try:
        wait(lambda: controller.status()['state'] == 'paused' and controller._worker is not None)
        yield controller, root / 'qwen35_single'
    finally:
        controller.submit('exit')
        try:
            assert controller.done.wait(15), controller.read_events()
        finally:
            host.close()


def completed(controller, command):
    identity = controller.submit(command)
    def poll():
        receipt = controller.get_operation(identity)
        return receipt if receipt and receipt['status'] in ('succeeded', 'failed', 'cancelled') else None
    return wait(poll)


def test_qwen_entry_runs_and_reloads_with_real_worker(qwen_controller):
    controller, directory = qwen_controller
    old = controller._worker
    assert completed(controller, 'resume')['status'] == 'succeeded'
    wait(lambda: controller.status()['consumed'] >= 23)
    statistics = directory / 'weights/stats.json'
    original = statistics.read_text()
    statistics.write_text('{}')
    model = directory / 'model.json'
    assert completed(controller, f'switch-model "{model}"')['status'] == 'failed'
    assert controller._worker is old and old.runnable
    statistics.write_text(original)
    set_controls(directory, value=33)
    assert completed(controller, f'switch-model "{model}"')['status'] == 'succeeded'
    assert controller.status()['state'] == 'paused'
    assert old.cleanup_confirmed and not old.has_readers
    assert controller._worker.runtime_id != old.runtime_id
    rows = events(directory)
    loads = [i for i, row in enumerate(rows) if row['phase'] == 'load']
    assert len(loads) == 2
    close = next(i for i, row in enumerate(rows) if row['phase'] == 'close')
    assert loads[0] < close < loads[1]
    assert controller.registry.get('qwen35_single').config['params']['model_config_path'] == str(model)
    assert completed(controller, 'save-config policy')['status'] == 'succeeded'
    assert json.loads((directory / 'policy.json').read_text())['params']['model_config_path'] == str(model)
