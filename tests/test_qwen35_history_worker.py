"""Actual RC/UDS worker/shared-image lifecycle with the production policy entry."""
import json
import os
import shutil
import time

import numpy as np
import pytest

from config_helpers import read_config
from history_helpers import ROOT, model_file
from qwen_helpers import wait


def completed(controller, command):
    identity = controller.submit(command)
    def done():
        result = controller.get_operation(identity)
        return result if result and result['status'] in ('succeeded','failed','cancelled') else None
    return wait(done)


def events(directory, phase):
    path = directory / 'events.jsonl'
    if not path.exists(): return []
    return [row for line in path.read_text().splitlines(keepends=True) if line.endswith('\n')
            for row in [json.loads(line)] if row['phase'] == phase]


@pytest.fixture(params=[False, True])
def running(tmp_path, monkeypatch, request):
    from robot_control.controller import RobotController
    from robot_control.robots import MockRobot
    from host_helpers import LocalPolicyProcess
    source = ROOT / 'policy/qwen35_history/main.py'
    assert source.exists(), 'production entry missing'
    root = tmp_path / 'policy/qwen35_history'
    root.mkdir(parents=True)
    manifest = read_config(ROOT / 'policy/qwen35_history/policy.yaml.example')
    manifest['params']['model_config_path'] = str(model_file(root, x=5, pipelined=request.param))
    (root / 'policy.yaml').write_text(json.dumps(manifest))
    (root / 'compose.yaml').write_text('services:\n  policy:\n    image: fixture:local\n')
    (root / 'main.py').write_text(source.read_text()+'''\nfrom .test_backend import factory as HistoryPolicy
_production_policy = policy
def policy(context, state, action_chunk, images, config, *, runtime):
    assert all(not image.flags.writeable for image in images.values())
    return _production_policy(context, state, action_chunk, images, config, runtime=runtime)
''')
    shutil.copyfile(ROOT / 'tests/fixtures/history_test_backend.py', root / 'test_backend.py')
    (root / 'controls.json').write_text(json.dumps({'value': .1}))
    package = str(ROOT / 'packages/qwen35-history-policy/src')
    monkeypatch.setenv('PYTHONPATH', package+os.pathsep+os.environ.get('PYTHONPATH',''))
    class CameraRobot(MockRobot):
        def read_images(self):
            return {name: (np.full((4,5,3),value,np.uint8),time.monotonic())
                    for name,value in [('head',11),('left_wrist',22),('right_wrist',33)]}
    rc = read_config(ROOT / 'configs/rc.mock.yaml.example')
    rc['safety']['enabled'] = False
    rc.update(policy_directory=str(root.parent), startup_policy='qwen35_history',
              outputs_directory=str(tmp_path / 'outputs'), frequency_hz=100,
              timeouts={'initialize_s': 5,'inference_s': 4,'release_s': 2})
    path = tmp_path / 'rc.yaml'
    path.write_text(json.dumps(rc))
    controller = RobotController(path, robot=CameraRobot(), process_factory=LocalPolicyProcess)
    controller.start()
    try:
        wait(lambda: controller.status()['state'] == 'paused' and controller._worker is not None)
        yield controller, root
    finally:
        controller.submit('exit')
        assert controller.done.wait(15), controller.read_events()


def test_consumption_history_survives_shared_image_release_and_pause(running):
    controller, root = running
    assert completed(controller,'resume')['status'] == 'succeeded'
    wait(lambda: controller.status()['consumed'] >= 32)
    assert completed(controller,'pause')['status'] == 'succeeded'
    with_history = [row for row in events(root,'predict_end') if row['indices']]
    assert with_history and all(value==11 for row in with_history for value in row['head'])
    old = len(events(root,'predict_end'))
    assert completed(controller,'resume')['status'] == 'succeeded'
    wait(lambda: len(events(root,'predict_end'))>old)
    assert events(root,'predict_end')[old]['indices'] == []
    assert controller._worker.runnable


def test_hot_task_discards_inflight_work_and_cold_reload_releases_instance(running):
    controller, root = running
    worker = controller._worker
    assert completed(controller,'resume')['status'] == 'succeeded'
    wait(lambda: controller.status()['consumed'] >= 20)
    assert completed(controller,'policy qwen35_history switch-task "new task"')['status'] == 'succeeded'
    fresh = wait(lambda: next((r for r in events(root,'predict_end') if r['task']=='new task'), None))
    assert fresh['indices'] == [] and controller._worker is worker
    assert len(events(root,'load')) == 1
    assert completed(controller,'pause')['status'] == 'succeeded'
    assert completed(controller,f'policy qwen35_history switch-model "{root / "model.json"}"')['status'] == 'succeeded'
    assert controller.status()['state'] == 'paused'
    assert worker.cleanup_confirmed and not worker.has_readers
    assert len(events(root,'close')) == 1 and len(events(root,'load')) == 2
