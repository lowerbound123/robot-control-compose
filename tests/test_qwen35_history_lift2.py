"""The normal LIFT2 history policy keeps its own gripper output and lifecycle."""
import copy
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np
import pytest

from config_helpers import read_config
from project_templates import copy_templates
from test_qwen35_history_runtime import Backend, config as runtime_config, frames

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'policy/qwen35_history_lift2'
sys.path.insert(0, str(ROOT / 'policy/qwen35_history/src'))
ARM_COLUMNS = [0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12]


def entry():
    path = SOURCE / 'main.py'
    assert path.is_file(), 'qwen35_history_lift2 must be a normal policy source'
    spec = importlib.util.spec_from_file_location('history_lift2_entry', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gripper_threshold_only_changes_output_copy_and_preserves_original_policy():
    from rc_qwen35_history.runtime import HistoryPolicy as Original
    module = entry()
    actions = np.arange(84, dtype=np.float32).reshape(6, 14) / 100
    actions[:, 6] = [-3., 0., 2.499, 2.5, 2.501, 4.9]
    actions[:, 13] = [4.9, 2.501, 2.5, 2.499, 0., -3.]
    before = actions.copy()
    actions.flags.writeable = False
    output = module.HistoryPolicy._process(actions)
    np.testing.assert_array_equal(output[:, ARM_COLUMNS], before[:, ARM_COLUMNS])
    np.testing.assert_allclose(output[:, 6], [0., 0., 0., 0., 4.9, 4.9])
    np.testing.assert_allclose(output[:, 13], [4.9, 4.9, 0., 0., 0., 0.])
    np.testing.assert_array_equal(actions, before)
    assert not np.shares_memory(output, actions)
    original = Original._process(actions)
    np.testing.assert_allclose(original[:, 6], [-3., .04, .04, .04, .04, .04])
    assert module.HistoryPolicy is not Original


@pytest.mark.parametrize('pipelined', [False, True])
def test_history_cadence_and_raw_canonical_are_unchanged(pipelined):
    module = entry()
    backend = Backend()
    backend.output[:, [6, 13]] = [2.5, 3.]
    context = module.HistoryPolicy(runtime_config(25, pipelined), backend=backend)
    from types import SimpleNamespace
    remaining, previous = [], 0
    try:
        for count in (0, 0, 1, 2, 10, 20):
            if len(remaining):
                remaining = remaining[count-previous:]
            remaining = context.step({'qpos': np.zeros(14, np.float32)}, remaining, frames(count),
                                     runtime=SimpleNamespace(consumed_actions=count, control_epoch=1))
            np.testing.assert_array_equal(remaining[:, 6], 0.)
            np.testing.assert_allclose(remaining[:, 13], 4.9)
            np.testing.assert_array_equal(context.canonical[:, 6], 2.5)
            np.testing.assert_array_equal(context.canonical[:, 13], 3.)
            assert context.history.tick == count
            previous = count
        assert len(backend.calls) == 1
    finally:
        backend.gate.set()
        context.close()


def test_independent_configuration_and_shared_dependency_image_are_discoverable(tmp_path):
    from robot_control.config import PolicyRegistry
    from robot_control.yaml_config import load_yaml_config
    assert SOURCE.is_dir(), 'LIFT2 source must exist'
    root = tmp_path / 'policy'
    for source in ('qwen35_history', 'qwen35_history_lift2'):
        copy_templates(ROOT / 'policy' / source, root / source)
    registry = PolicyRegistry(root)
    records = [registry.get(name) for name in ('qwen35_history', 'qwen35_history_lift2')]
    assert records[0].directory != records[1].directory
    assert records[1].config['id'] == 'qwen35_history_lift2'
    assert records[1].config['execution']['control_mode'] == 'qpos'
    assert (SOURCE / 'model.json.example').read_bytes() == (ROOT / 'policy/qwen35_history/model.json.example').read_bytes()
    service = load_yaml_config(SOURCE / 'compose.yaml')['services']['policy']
    assert service['image'] == 'robot-control-compose-qwen35-history:local'
    assert service['build']['dockerfile'] == 'policy/qwen35_history_lift2/Dockerfile'
    assert (SOURCE / service['build']['additional_contexts']['policy-deps']).resolve() == SOURCE / 'dependencies'
    assert service['restart'] == 'no'
    assert json.loads(service['environment']['RC_SOURCE_MODULES'])['rc_qwen35_history_lift2'] == '/workspace/policy/qwen35_history_lift2/src'
    assert all(mount['read_only'] for mount in service['volumes'] if mount['target'] != '/data/huggingface')


def test_configure_targets_the_new_policy_without_modifying_original_config():
    module = entry()
    active = read_config(SOURCE / 'policy.yaml.example')
    candidate = copy.deepcopy(active)
    effects = []
    module.configure(None, candidate, 'params.task', '"Lift2 task"', effects.append)
    assert candidate['params']['task'] == 'Lift2 task' and effects == ['reset']
    assert active['params']['task'] is None
    assert module.POLICY_ROOT == SOURCE


def test_scope_selects_the_new_source_and_shared_history_tests():
    spec = importlib.util.spec_from_file_location('lift2_history_scope', ROOT / 'scripts/test_scope.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert 'qwen35_history_lift2' in module.POLICIES
    selected = module.suite({'qwen35_history_lift2'}, False)
    assert 'tests/test_qwen35_history_lift2.py' in selected
    assert 'tests/test_qwen35_history_runtime.py' in selected
    assert 'tests/test_qwen35_backend.py' not in selected
    assert 'tests/test_qwen35_history_lift2.py' not in module.suite({'qwen35_history'}, False)


@pytest.fixture(params=[False, True])
def running_lift2(tmp_path, monkeypatch, request):
    import os
    import shutil
    from connector_helpers import MockRobot
    from history_helpers import model_file
    from host_helpers import LocalPolicyProcess
    from robot_control.controller import RobotController
    from test_controller import until
    source = entry()
    directory = tmp_path / 'policy/qwen35_history_lift2'
    directory.mkdir(parents=True)
    manifest = read_config(SOURCE / 'policy.yaml.example')
    manifest['params']['model_config_path'] = str(model_file(directory, x=5, pipelined=request.param,
                                                          robot_type='arx_lift2'))
    (directory / 'policy.yaml').write_text(json.dumps(manifest))
    (directory / 'compose.yaml').write_text('services:\n  policy:\n    image: fixture:local\n')
    (directory / 'main.py').write_text(Path(source.__file__).read_text() + '''
from .test_backend import Model
_ProductionHistoryPolicy = HistoryPolicy
class Lift2TestModel(Model):
    def predict(self, *args):
        actions, timings = super().predict(*args)
        actions[:, 6] = 2.5
        actions[:, 13] = 3.0
        return actions, timings
def HistoryPolicy(config, *, policy_config, project_root):
    root = Path(policy_config['params']['model_config_path']).parent
    return _ProductionHistoryPolicy(config, policy_config=policy_config,
                                    backend=Lift2TestModel(root), project_root=project_root)
''')
    shutil.copyfile(ROOT / 'tests/fixtures/history_test_backend.py', directory / 'test_backend.py')
    (directory / 'controls.json').write_text(json.dumps({'value': .1}))
    monkeypatch.setenv('PYTHONPATH', str(SOURCE / 'src') + os.pathsep + os.environ.get('PYTHONPATH', ''))
    config = read_config(ROOT / 'configs/rc.mock.yaml.example')
    config['safety']['enabled'] = False
    config.update(policy_directory=str(directory.parent), startup_policy='qwen35_history_lift2',
                  outputs_directory=str(tmp_path / 'outputs'), frequency_hz=100,
                  timeouts={'initialize_s': 5, 'inference_s': 4, 'release_s': 2})
    path = tmp_path / 'rc.yaml'
    path.write_text(json.dumps(config))
    controller = RobotController(path, robot=MockRobot({'image_size': [4, 5]}), process_factory=LocalPolicyProcess)
    controller.start()
    try:
        until(lambda: controller.status()['state'] == 'paused' and controller._worker is not None, 10)
        yield controller, directory
    finally:
        controller.submit('exit')
        assert controller.done.wait(15), controller.read_events()
        assert controller.exit_code == 0 and controller._pool._bytes == 0


def test_real_worker_then_example_self_chain_keep_lift2_grippers_and_independent_instances(running_lift2):
    from test_controller import until
    from test_example_controller import command
    controller, directory = running_lift2
    command(controller, 'resume')
    until(lambda: controller.status()['consumed'] >= 4)
    command(controller, 'pause')
    commands = [row for row in controller.robot.commands if np.isclose(row[0], .1)]
    assert commands
    np.testing.assert_allclose(np.asarray(commands)[:, ARM_COLUMNS], .1)
    np.testing.assert_array_equal(np.asarray(commands)[:, 6], 0.)
    np.testing.assert_allclose(np.asarray(commands)[:, 13], 4.9)
    original_worker = controller._worker
    command(controller, 'policy qwen35_history_lift2 config set params.task "LIFT2 test"')
    assert controller._worker is original_worker
    copy_templates(ROOT / 'policy/example', directory.parent / 'example')
    chain = directory.parent / 'history_lift2_chain'
    chain.mkdir()
    (chain / 'policy.yaml').write_text(json.dumps({'schema_version': 3, 'kind': 'chain',
        'policies': ['qwen35_history_lift2', 'example', 'example']}))
    command(controller, 'policy switch history_lift2_chain')
    members = controller._worker.member_workers()
    assert set(members) == {'qwen35_history_lift2', 'example-0', 'example-1'}
    assert len({worker.pid for worker in members.values()}) == 3
    baseline = controller.status()['consumed']
    command(controller, 'resume')
    until(lambda: controller.status()['consumed'] >= baseline + 4)
    command(controller, 'pause')
    assert all(worker.runnable for worker in members.values())
    np.testing.assert_allclose(controller.robot.commands[-1][[6, 13]], [0., 4.9])
