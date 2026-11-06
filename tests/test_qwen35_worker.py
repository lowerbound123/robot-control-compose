"""Real RC, UDS host, production Qwen entry and scheduler; CUDA is substituted."""
from config_helpers import read_config
from project_templates import template_path, copy_templates
import json
import os
import time
from pathlib import Path

import pytest

from robot_control.controller import RobotController
from robot_control.robots import MockRobot
from qwen_helpers import ROOT, events, prepare_policies, set_controls, wait


class ThreeCameraRobot(MockRobot):
    def read_images(self):
        import numpy as np
        return {name: (np.full((8, 8, 3), i, np.uint8), time.monotonic())
                for i, name in enumerate(('head', 'left_wrist', 'right_wrist'), 1)}


@pytest.fixture(params=('qwen35_single', 'qwen35_twostage'))
def qwen_controller(tmp_path, monkeypatch, request):
    package = str(ROOT / 'packages/qwen35-policy/src')
    monkeypatch.setenv('PYTHONPATH', package + os.pathsep + os.environ.get('PYTHONPATH', ''))
    identity = request.param
    root = prepare_policies(tmp_path / 'policy', (identity,))
    rc = read_config(template_path(ROOT / 'configs/rc.mock.yaml'))
    rc['safety']['enabled'] = False  # Deterministic test actions encode the policy identity.
    rc.update(policy_directory=str(root), startup_policy=identity,
              outputs_directory=str(tmp_path / 'outputs'), frequency_hz=100,
              timeouts={'initialize_s': 5, 'inference_s': 3, 'release_s': 1})
    path = tmp_path / 'rc.yaml'
    path.write_text(json.dumps(rc))
    from host_helpers import LocalPolicyProcess
    controller = RobotController(path, robot=ThreeCameraRobot(), process_factory=LocalPolicyProcess)
    controller.start()
    try:
        wait(lambda: controller.status()['state'] == 'paused' and controller._worker is not None)
        yield controller, root / identity
    finally:
        controller.submit('exit')
        assert controller.done.wait(15), controller.read_events()


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
    assert completed(controller, f'policy {directory.name} switch-model "{model}"')['status'] == 'failed'
    assert controller._worker is old and old.runnable
    statistics.write_text(original)
    set_controls(directory, value=33)
    assert completed(controller, f'policy {directory.name} switch-model "{model}"')['status'] == 'succeeded'
    assert controller.status()['state'] == 'paused'
    assert old.cleanup_confirmed and not old.has_readers
    assert controller._worker.runtime_id != old.runtime_id
    rows = events(directory)
    loads = [i for i, row in enumerate(rows) if row['phase'] == 'load']
    assert len(loads) == 2
    close = next(i for i, row in enumerate(rows) if row['phase'] == 'close')
    assert loads[0] < close < loads[1]
    assert controller.registry.get(directory.name).config['params']['model_config_path'] == str(model)
    assert completed(controller, f'policy {directory.name} config save')['status'] == 'succeeded'
    assert read_config(directory / 'policy.yaml')['params']['model_config_path'] == str(model)


@pytest.mark.parametrize('task_command', ['config set params.task', 'switch-task'])
def test_task_hot_update_keeps_worker_and_model_and_persists_override(qwen_controller, task_command):
    controller, directory = qwen_controller
    worker = controller._worker
    runtime_id = worker.runtime_id
    version = worker.config_version
    model_config = (directory / 'model.json').read_text()
    assert completed(controller, 'resume')['status'] == 'succeeded'
    wait(lambda: controller.status()['consumed'] >= 23)
    assert events(directory, 'predicted')[-1]['instruction'] == 'test only'

    set_controls(directory, value=44)
    assert completed(controller, f'policy {directory.name} {task_command} "把杯子放到托盘里"')['status'] == 'succeeded'
    if task_command == 'config set params.task':
        assert controller.status()['state'] == 'paused'
        assert controller._worker is worker and worker.runtime_id == runtime_id
        assert completed(controller, 'resume')['status'] == 'succeeded'
    else:
        assert controller.status()['state'] == 'running'
    wait(lambda: any(row['instruction'] == '把杯子放到托盘里' for row in events(directory, 'predicted')))
    wait(lambda: controller.robot.commands[-1][0] == 44)
    assert controller.status()['state'] == 'running'
    assert controller._worker is worker and worker.runtime_id == runtime_id
    assert worker.config_version == version + 1
    assert controller.registry.get(directory.name).version == version + 1
    assert worker.config['params']['task'] == '把杯子放到托盘里'
    assert completed(controller, f'policy {directory.name} config save')['status'] == 'succeeded'
    assert read_config(directory / 'policy.yaml')['params']['task'] == '把杯子放到托盘里'
    assert (directory / 'model.json').read_text() == model_config

    assert completed(controller, 'pause')['status'] == 'succeeded'
    assert completed(controller, f'policy {directory.name} config set params.task null')['status'] == 'succeeded'
    before = len(events(directory, 'predicted'))
    assert completed(controller, 'resume')['status'] == 'succeeded'
    wait(lambda: len(events(directory, 'predicted')) > before)
    assert events(directory, 'predicted')[-1]['instruction'] == 'test only'
    assert controller._worker is worker and worker.config_version == version + 2
    rows = events(directory)
    assert len({row['pid'] for row in rows}) == 1
    assert len(events(directory, 'load')) == len(events(directory, 'warmup')) == 1
    assert not events(directory, 'close')


def test_switch_task_targets_only_the_selected_repeated_qwen_instance(qwen_controller):
    controller, directory = qwen_controller
    source = directory.name
    chain_path = directory.parent / 'qwen_test_chain/policy.yaml'
    manifest = read_config(chain_path)
    manifest['policies'] = [source, source]
    chain_path.write_text(json.dumps(manifest))
    assert completed(controller, 'policy switch qwen_test_chain')['status'] == 'succeeded'
    group = controller._worker
    workers = group.member_workers()
    first, target = workers[source + '-0'], workers[source + '-1']
    versions = (group.config_version, first.config_version, target.config_version)
    assert completed(controller, f'policy {source} switch-task ambiguous')['status'] == 'failed'
    assert completed(controller, f'policy {source}-1 switch-task "selected task"')['status'] == 'succeeded'
    assert controller._worker is group and group.member_workers() == workers
    assert first.config['params']['task'] is None
    assert target.config['params']['task'] == 'selected task'
    assert (group.config_version, first.config_version, target.config_version) == (
        versions[0] + 1, versions[1], versions[2] + 1)
    assert completed(controller, f'policy {source}-1 switch-task " "')['status'] == 'failed'
    assert target.runnable and target.config['params']['task'] == 'selected task'
    assert controller.status()['state'] == 'paused'
