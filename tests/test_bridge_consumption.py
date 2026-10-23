import numpy as np

from test_controller import setup_controller as setup_controller, ready, until


def test_metadata_counts_full_targets_only_during_slew(setup_controller):
    controller,robot,_=setup_controller
    controller.limiter.enabled=True
    worker=ready(controller)
    controller.registry.get('first').config['execution']['call_every_actions']=1
    controller.submit('resume')
    until(lambda:len(worker.requests)==1)
    first=worker.requests[-1]
    assert first['runtime']['consumed_actions']==0
    assert first['runtime']['control_epoch']==first['generation']
    assert 'observation_id' not in first['state']
    baseline=len(robot.commands)
    worker.finish(np.ones((2,14)))
    until(lambda:len(robot.commands)>=baseline+3)
    assert controller.status()['consumed']==0
    assert len(worker.requests)==1
    until(lambda:len(worker.requests)==2)
    assert worker.requests[-1]['runtime']['consumed_actions']==1
    assert worker.requests[-1]['runtime']['observation_id']!=first['runtime']['observation_id']


def test_hot_patch_waits_at_complete_action_boundary_before_next_slew(setup_controller):
    controller,robot,_=setup_controller
    worker=ready(controller)
    with controller._lock:
        controller.limiter.enabled=True
        controller.limiter.reset(np.zeros(14))
        controller.registry.get('first').config['execution']['call_every_actions']=1
        controller.buffer.install(np.stack([np.zeros(14),np.full(14,10.)]))
        controller.buffer.begin_request()
        controller._state='running'
        controller._needs_request=False
        controller._operation={'kind':'patch'}
        try:
            controller._tick()
            assert controller.buffer.consumed==1 and not worker.requests
            completed=len(robot.commands)
            controller._tick()
            assert len(robot.commands)==completed, 'patch must hold the completed target boundary'
            controller._operation=None
            controller._tick()
            assert len(robot.commands)==completed, 'overdue callback must precede the next slew target'
            assert worker.requests[-1]['runtime']['consumed_actions']==1
            np.testing.assert_array_equal(worker.requests[-1]['action_chunk'],np.full((1,14),10.))
        finally:
            controller._operation=None
            controller._state='paused'


def test_remote_forced_cleanup_makes_rc_exit_nonzero(setup_controller):
    controller,_,_=setup_controller
    worker=ready(controller)
    controller._external.begin_close=lambda worker:None
    controller._external.close_worker=lambda worker,timeout:True
    controller._close_worker(worker)
    assert controller.exit_code==1
    assert any('模型' in event['message'] and '强制' in event['message'] for event in controller.events.queue)
