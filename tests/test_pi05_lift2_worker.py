"""Production LIFT2 Pi05 callbacks with real RC/worker and owned test inference."""
import json
import os
from pathlib import Path

import numpy as np
import pytest

from config_helpers import read_config
from connector_helpers import MockRobot
from host_helpers import LocalPolicyProcess
from pi05_lift2_helpers import checkpoint_fixture
from project_templates import copy_templates
from robot_control.controller import RobotController
from test_controller import until
from test_example_controller import command

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT/'policy/pi05_lift2'

MODEL_BOUNDARY = '''
import numpy as np
_ProductionPolicy = AsyncPi05Policy
class FixtureBackend:
    def __init__(self, config, root):
        self.chunk_size = config['execution']['chunk_size']
        self.root = root
    def event(self, name):
        with (self.root/'events.jsonl').open('a') as stream:
            stream.write(json.dumps({'event': name})+'\\n')
    def load(self): self.event('load')
    def warmup(self): self.event('warmup')
    def reset_context(self): self.event('reset')
    def close(self): self.event('close')
    def predict(self, qpos, images, instruction):
        assert all(image.flags.owndata and image.flags.writeable for image in images.values())
        self.event('predict')
        actions = np.full((self.chunk_size, 14), .1, np.float32)
        actions[:, 0] = .2 if instruction == 'task B' else .1
        actions[:, 6], actions[:, 13] = 2.5, 2.
        return actions
def AsyncPi05Policy(config, *, policy_config, project_root):
    root = Path(policy_config['params']['model_config_path']).parent
    return _ProductionPolicy(config, policy_config=policy_config, project_root=project_root,
                             backend=FixtureBackend(config, root))
'''


@pytest.fixture
def running(tmp_path, monkeypatch):
    yield from running_source(tmp_path, monkeypatch, 'pi05_lift2')


def running_source(tmp_path, monkeypatch, source):
    from pi05_helpers import checkpoint_fixture as native_fixture
    policy_source = ROOT/'policy'/source
    folder = tmp_path/'policy'/source
    folder.mkdir(parents=True)
    path, *_ = (native_fixture if source == 'pi05' else checkpoint_fixture)(folder)
    manifest = read_config(policy_source/'policy.yaml.example')
    manifest['params']['model_config_path'] = str(path)
    (folder/'policy.yaml').write_text(json.dumps(manifest))
    (folder/'compose.yaml').write_text('services:\n  policy:\n    image: fixture:local\n')
    (folder/'main.py').write_text((policy_source/'main.py').read_text()+MODEL_BOUNDARY)
    monkeypatch.setenv('PYTHONPATH', str(policy_source/'src')+os.pathsep+os.environ.get('PYTHONPATH',''))
    config = read_config(ROOT/'configs/rc.mock.yaml.example')
    config['safety']['enabled'] = False
    config.update(policy_directory=str(folder.parent), startup_policy=source,
                  outputs_directory=str(tmp_path/'outputs'), frequency_hz=30,
                  timeouts={'initialize_s':10,'inference_s':5,'release_s':3})
    rc_config = tmp_path/'rc.yaml'
    rc_config.write_text(json.dumps(config))
    rc = RobotController(rc_config, robot=MockRobot({'image_size':[4,5]}), process_factory=LocalPolicyProcess)
    rc.start()
    try:
        until(lambda:rc._worker is not None and rc.status()['state']=='paused',10)
        yield rc, folder
    finally:
        rc.submit('exit')
        assert rc.done.wait(15),rc.status()
        assert rc.exit_code==0 and rc._pool._bytes==0


def events(folder):
    return [json.loads(line)['event'] for line in (folder/'events.jsonl').read_text().splitlines()]


@pytest.mark.parametrize('mode', ['sync','async'])
def test_lift2_worker_hot_grippers_save_pause_and_resume(running, mode):
    rc, folder = running
    command(rc, f'policy pi05_lift2 config set execution.mode {mode}')
    command(rc, 'resume')
    until(lambda:rc.buffer.consumed>=4,5)
    worker = rc._worker
    np.testing.assert_allclose(rc.robot.commands[-1][[6,13]], [4.9,0.])
    for key,value,expected in [
        ('gripper_upper_threshold',3.,[0.,0.]),
        ('gripper_upper_value',4.,[0.,0.]),
        ('gripper_lower_threshold',2.,[2.5,0.]),
        ('gripper_lower_value',.25,[2.5,.25]),
        ('gripper_upper_threshold',2.5,[4.,.25]),
    ]:
        index = len(rc.robot.commands)
        command(rc, f'policy pi05_lift2 config set params.{key} {value}')
        def arrived():
            assert rc.status()['state'] != 'fault', rc.read_events()
            return any(np.allclose(row[[6,13]],expected) for row in rc.robot.commands[index:])
        until(arrived,5)
        assert rc._worker is worker and rc.status()['state']=='running'
    command(rc, 'policy pi05_lift2 config save')
    saved = read_config(folder/'policy.yaml')['params']
    assert [saved[key] for key in ('gripper_upper_threshold','gripper_upper_value','gripper_lower_threshold','gripper_lower_value')] == [2.5,4.,2.,.25]
    command(rc, 'pause')
    consumed = rc.buffer.consumed
    command(rc, 'resume')
    until(lambda:rc.buffer.consumed>consumed,5)
    assert rc._worker is worker and events(folder).count('load')==1


def test_lift2_runs_before_example_self_chain_and_releases_old_reader(running):
    rc, folder = running
    copy_templates(ROOT/'policy/example', folder.parent/'example')
    chain = folder.parent/'lift2_chain'
    chain.mkdir()
    (chain/'policy.yaml').write_text(json.dumps({'schema_version':3,'kind':'chain',
        'policies':['pi05_lift2','example','example']}))
    old = rc._worker
    command(rc, 'policy switch lift2_chain')
    assert old.cleanup_confirmed and not old.has_readers
    members = rc._worker.member_workers()
    assert set(members)=={'pi05_lift2','example-0','example-1'}
    assert len({worker.pid for worker in members.values()})==3
    command(rc, 'resume')
    until(lambda:rc.buffer.consumed>=4,5)
    command(rc, 'pause')
    np.testing.assert_allclose(rc.robot.commands[-1][[6,13]], [4.9,0.])
    assert all(worker.runnable for worker in members.values())
