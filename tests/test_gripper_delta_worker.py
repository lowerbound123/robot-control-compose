"""Hot signed offsets keep their type through the real RC/worker save boundary."""
import numpy as np
import pytest

from config_helpers import read_config
from test_controller import until
from test_example_controller import command
from test_pi05_lift2_worker import running as running_pi05
from test_qwen35_history_lift2 import running_lift2
from test_qwen35_history_worker import running as running_history


@pytest.mark.parametrize('mode', ['sync','async'])
def test_history_delta_hot_updates_save_reload_and_pause(running_history, mode):
    exercise_worker(running_history,mode,'qwen35_history',[-1.,-1.])


@pytest.mark.parametrize('mode', ['sync','async'])
def test_history_lift2_delta_hot_updates_save_reload_and_pause(running_lift2, mode):
    exercise_worker(running_lift2,mode,'qwen35_history_lift2',[2.5,3.])


@pytest.mark.parametrize('mode', ['sync','async'])
def test_pi05_lift2_delta_hot_updates_save_reload_and_pause(running_pi05, mode):
    exercise_worker(running_pi05,mode,'pi05_lift2',[2.5,2.])


def exercise_worker(running, mode, source, raw):
    rc, folder = running
    raw = np.asarray(raw)
    worker = rc._worker
    command(rc, f'policy {source} config set execution.mode {mode}')
    for key,value in [('gripper_upper_threshold','0'), ('gripper_lower_threshold','0'),
                      ('gripper_upper_value','"+0.5"'), ('gripper_lower_value','"-0.5"')]:
        command(rc, f'policy {source} config set params.{key} {value}')
    command(rc,'resume')
    expected = np.where(raw >= 0, raw+.5, raw-.5)
    until(lambda: rc.buffer.consumed >= 8, 5)
    np.testing.assert_allclose(rc.robot.commands[-1][[6,13]],expected)
    command(rc,'pause')
    command(rc,f'policy {source} config set params.gripper_upper_value -0.5')
    assert read_live(rc,source)['gripper_upper_value'] == -.5
    command(rc,f'policy {source} config set params.gripper_upper_value "-0.25"')
    command(rc,f'policy {source} config set params.gripper_lower_value "+0.25"')
    command(rc,f'policy {source} config save')
    saved = read_config(folder/'policy.yaml')['params']
    assert saved['gripper_upper_value']=='-0.25' and saved['gripper_lower_value']=='+0.25'
    assert rc._worker is worker
    expected = np.where(raw >= 0, raw-.25, raw+.25)
    before=rc.buffer.consumed
    command(rc,'resume')
    until(lambda:rc.buffer.consumed>=before+8,5)
    np.testing.assert_allclose(rc.robot.commands[-1][[6,13]],expected)
    command(rc,'pause')
    command(rc,f'policy switch {source}')
    assert worker.cleanup_confirmed and not worker.has_readers
    assert rc._worker is not worker and rc.status()['state']=='paused'
    assert read_live(rc,source)['gripper_upper_value']=='-0.25'
    before=rc.buffer.consumed
    command(rc,'resume')
    until(lambda:rc.buffer.consumed>=before+5,5)
    np.testing.assert_allclose(rc.robot.commands[-1][[6,13]],expected)


def read_live(controller, source):
    return controller.query(f'policy {source} config show')['params']
