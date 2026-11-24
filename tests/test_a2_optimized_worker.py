"""Prove the async worker replies and releases shared images while decode is held."""
import json
import shutil

import numpy as np
import pytest

from config_helpers import read_config
from history_helpers import ROOT, model_file
from host_helpers import LocalPolicyProcess
from qwen_helpers import wait
from rc_policy_sdk.images import ImagePool


@pytest.fixture
def held_language_policy(tmp_path):
    source = ROOT / 'policy/a2_optimized'
    directory = tmp_path / source.name
    directory.mkdir()
    for name in ('main.py', 'runtime.py'):
        shutil.copyfile(source / name, directory / name)
    shutil.copyfile(ROOT / 'tests/fixtures/history_test_backend.py', directory / 'test_backend.py')
    with (directory / 'main.py').open('a') as file:
        file.write('''
from .test_backend import Model
from .runtime import OptimizedPolicy as RealPolicy
import time
import numpy as np

class HeldLanguage(Model):
    def predict(self, *args):
        actions, timings = super().predict(*args)
        actions[:, 0] = self.calls / 8
        actions[:, 1] = np.arange(len(actions)) / 128
        return actions, timings

    def refresh_language(self):
        self.event('language_start')
        deadline = time.monotonic() + 8
        while not (self.root / 'continue').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('test did not release language generation')
            time.sleep(.01)
        yield
        self.event('language_end')

def OptimizedPolicy(config, *, policy_config, project_root):
    root = Path(policy_config['params']['model_config_path']).parent
    return RealPolicy(config, backend=HeldLanguage(root), policy_config=policy_config, project_root=project_root)
''')
    path = model_file(directory, x=8)
    value = json.loads(path.read_text())
    value['inference'] = {'generate_language': ['Subtask']}
    path.write_text(json.dumps(value))
    (directory / 'controls.json').write_text('{}')
    manifest = read_config(source / 'policy.yaml.example')
    manifest['params']['model_config_path'] = str(path)
    manifest['execution']['mode'] = 'async'
    (directory / 'policy.yaml').write_text(json.dumps(manifest))
    shutil.copyfile(source / 'compose.yaml', directory / 'compose.yaml')
    return directory, manifest


def test_fourth_result_crosses_worker_socket_before_language_finishes(tmp_path, held_language_policy):
    directory, manifest = held_language_policy
    worker = LocalPolicyProcess(directory, manifest, init_timeout_s=10, inference_timeout_s=5, release_timeout_s=3)
    cameras = ('head', 'left_wrist', 'right_wrist')
    pool = ImagePool(tmp_path / 'frames', cameras=cameras)
    for name in cameras:
        pool.publish(name, np.zeros((3, 4, 3), np.uint8), 1.)
    try:
        worker.start()
        for n in range(1, 5):
            lease = pool.acquire()
            try:
                worker.submit(dict(request_id=n, generation=1, config_version=1,
                                   state={'qpos': [0.]*14}, action_chunk=[], images=lease.descriptors,
                                   runtime={'observation_id': str(n), 'consumed_actions': n, 'control_epoch': 1}))
                reply = wait(worker.poll, timeout=4)
                assert reply['type'] == 'result' and len(reply['actions']) == 50
                assert all(action[0] == n / 8 for action in reply['actions'])
                assert reply['images_released']
            except BaseException:
                (directory / 'continue').touch()
                worker.close()
                assert worker.cleanup_confirmed
                raise
            finally:
                lease.release()
        journal = directory / 'events.jsonl'
        wait(lambda: 'language_start' in journal.read_text())
        assert 'language_end' not in journal.read_text()
        (directory / 'continue').touch()
        wait(lambda: 'language_end' in journal.read_text())
    finally:
        (directory / 'continue').touch()
        worker.close()
        assert worker.cleanup_confirmed
        pool.close()


def test_async_rc_sends_fourth_chunk_while_language_is_held(tmp_path, held_language_policy):
    from robot_control.controller import RobotController
    from test_qwen35_worker import ThreeCameraRobot, completed

    directory, _ = held_language_policy
    rc = read_config(ROOT / 'configs/rc.mock.yaml.example')
    rc.update(policy_directory=str(directory.parent), startup_policy=directory.name,
              outputs_directory=str(tmp_path / 'outputs'), frequency_hz=30)
    robot = ThreeCameraRobot()
    config = tmp_path / 'rc.yaml'
    config.write_text(json.dumps(rc))
    controller = RobotController(config, robot=robot, process_factory=LocalPolicyProcess)
    controller.start()
    try:
        wait(lambda: controller.status()['state'] == 'paused' and controller._worker is not None)
        assert controller._worker.config['execution']['mode'] == 'async'
        assert completed(controller, 'resume')['status'] == 'succeeded'
        journal = directory / 'events.jsonl'
        wait(lambda: journal.exists() and 'language_start' in journal.read_text())
        # First joint identifies the prediction, second joint identifies the frame.
        # Distinct frames rule out both old buffered chunks and a repeated hold.
        def fourth_frames():
            return {cmd[1] for cmd in robot.commands.copy() if cmd[0] == 4 / 8}
        initial = fourth_frames()
        wait(lambda: len(fourth_frames() - initial) >= 3, timeout=4)
        assert 'language_end' not in journal.read_text()
        (directory / 'continue').touch()
        wait(lambda: 'language_end' in journal.read_text())
    finally:
        (directory / 'continue').touch()
        controller.submit('exit')
        assert controller.done.wait(15), controller.read_events()
