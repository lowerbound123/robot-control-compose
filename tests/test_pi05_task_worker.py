"""Both Pi05 sources expose task/gripper controls through the actual RC/SDK."""
import numpy as np
import pytest

from config_helpers import read_config
from test_example_controller import command
from test_controller import until
from test_pi05_lift2_worker import running_source, events


@pytest.fixture(params=['pi05','pi05_lift2'])
def running(request,tmp_path,monkeypatch):
    for rc,folder in running_source(tmp_path,monkeypatch,request.param):
        yield request.param,rc,folder


@pytest.mark.parametrize('mode',['sync','async'])
def test_switch_task_and_grippers_keep_worker_then_save_reload_correctly(running,mode):
    source,rc,folder=running
    worker=rc._worker
    command(rc,f'policy {source} config set execution.mode {mode}')
    command(rc,f'policy {source} switch-task "paused task"')
    assert rc.status()['state']=='paused' and rc.buffer.consumed==0
    assert rc._worker is worker and events(folder).count('load')==1
    command(rc,f'policy {source} config set params.task null')
    assert rc.status()['state']=='paused'
    command(rc,'resume')
    until(lambda:rc.buffer.consumed>=4,5)
    command(rc,f'policy {source} switch-task "task B"')
    assert rc._worker is worker
    if rc.status()['state']=='paused':command(rc,'resume')
    until(lambda:rc.robot.commands and np.isclose(rc.robot.commands[-1][0],.2),5)
    for key,value in [('gripper_upper_threshold','2'),('gripper_upper_value','"+0.5"'),
                      ('gripper_lower_threshold','0'),('gripper_lower_value','"-0.25"')]:
        command(rc,f'policy {source} config set params.{key} {value}')
    until(lambda:np.allclose(rc.robot.commands[-1][[6,13]],[3.,2.5]),5)
    assert rc._worker is worker and events(folder).count('load')==1
    command(rc,'pause')
    command(rc,f'policy {source} config save')
    saved=read_config(folder/'policy.yaml')['params']
    assert saved['task']=='task B'
    assert saved['gripper_upper_value']=='+0.5' and saved['gripper_lower_value']=='-0.25'
    command(rc,f'policy switch {source}')
    assert worker.cleanup_confirmed and not worker.has_readers
    command(rc,'resume')
    until(lambda:rc.buffer.consumed>=4 and np.isclose(rc.robot.commands[-1][0],.2),5)
    np.testing.assert_allclose(rc.robot.commands[-1][[6,13]],[3.,2.5])
    new_worker=rc._worker
    command(rc,f'policy {source} config set params.task null')
    if rc.status()['state']=='paused':command(rc,'resume')
    until(lambda:np.isclose(rc.robot.commands[-1][0],.1),5)
    assert rc._worker is new_worker and events(folder).count('load')==2
