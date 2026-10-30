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


def test_remote_forced_cleanup_makes_rc_exit_nonzero(setup_controller):
    controller,_,_=setup_controller
    worker=ready(controller)
    # Remote resources are force-reclaimed through the external-resource seam.
    controller._external.begin_close=lambda worker, **kwargs:None
    controller._external.close_worker=lambda worker, **kwargs:True
    controller._close_worker(worker)
    assert controller.exit_code==1
    assert any(event['level']=='warning' for event in controller.events.queue), \
        'forced cleanup must be reported as a structured warning event'
