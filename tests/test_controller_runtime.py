import json
import time

import pytest

from test_controller import setup_controller, FakeProcess, until
from test_runtime_manager import manager


class RuntimeWorker(FakeProcess):
    release_error = None
    supports_commands = False
    supports_observe = False
    def stop_commands(self): pass
    def resume_commands(self): pass
    def close(self, *, deadline=None):
        if self.config['params'].get('release_fail'):
            raise RuntimeError('SDK release failed')
        return super().close()
    def start(self, cancel_event=None):
        super().start(cancel_event)
        if self.config['params'].get('init_fail'):
            raise RuntimeError('initialize failed')
    def accept_exit_proof(self, proof):
        assert proof.authority is self.kwargs['exit_authority']
        assert proof.container_id == self.kwargs['endpoint'].container_id
        self.alive=False


@pytest.fixture
def runtime_controller(setup_controller):
    controller,robot,root=setup_controller
    runtime,engine=manager(root)
    runtime.wait_ready=lambda *a:None
    controller.runtime=runtime
    controller.process_factory=RuntimeWorker
    controller._external.deployment_id='deployment1'
    for path in (root/'policy').glob('*/compose.yaml'):
        path.write_text(json.dumps({'services':{'policy':{'image':'fixture:local'}}}))
    return controller,engine,root


def ready(case):
    controller,engine,root=case
    controller.start()
    until(lambda:controller.status()['policy']=='first')
    return controller,engine,root


def operation(controller,command,expected='succeeded'):
    receipt=controller.submit(command)
    until(lambda:controller.get_operation(receipt)['status'] in ('succeeded','failed','cancelled'),8)
    result=controller.get_operation(receipt)
    assert result['status']==expected,result
    return result


def test_old_container_removed_before_same_policy_new_create(runtime_controller):
    controller,engine,_=ready(runtime_controller)
    old=controller._worker
    operation(controller,'switch first')
    assert controller._worker is not old
    assert [kind for kind,_ in engine.events]==['create','start','stop','rm','create','start']
    assert controller.status()['state']=='paused'


def test_sdk_failure_still_removes_container_and_preserves_forced_fact(runtime_controller):
    controller,engine,_=ready(runtime_controller)
    controller._worker.config['params']['release_fail']=True
    operation(controller,'switch second')
    assert [kind for kind,_ in engine.events]==['create','start','stop','rm','create','start']
    assert controller.exit_code==1
    assert controller.status()['runtime']['cleanup_forced']


def test_new_initialize_failure_cleans_container_and_leaves_empty(runtime_controller):
    controller,engine,root=ready(runtime_controller)
    path=root/'policy/second/policy.json';value=json.loads(path.read_text());value['params']['init_fail']=True;path.write_text(json.dumps(value))
    operation(controller,'switch second','failed')
    assert not engine.containers
    assert controller.status()['policy'] is None
    assert controller.status()['runtime']['phase']=='FAULT_EMPTY'


def test_unconfirmed_cleanup_blocks_new_group_and_keeps_mcp_owner(runtime_controller):
    controller,engine,_=ready(runtime_controller)
    operation(controller,'take')
    # A daemon failure after static preparation cannot be mistaken for exit.
    original=controller.runtime.stop_and_remove
    def fail(*a):raise RuntimeError('daemon lost during cleanup')
    controller.runtime.stop_and_remove=fail
    operation(controller,'switch second','failed')
    assert len(engine.containers)==1
    assert controller.status()['runtime']['phase']=='BLOCKED_CLEANUP'
    assert controller.control_owner=='mcp'
    controller.runtime.stop_and_remove=original


def test_chain_intersection_is_destroyed_and_duplicate_member_uses_one_container(runtime_controller):
    controller,engine,root=ready(runtime_controller)
    directory=root/'policy/default_chain';directory.mkdir()
    path=directory/'policy.json';path.write_text(json.dumps(dict(schema_version=3,kind='chain',policies=['first','second','first'])))
    operation(controller,'switch default_chain')
    assert len(engine.containers)==2
    old_ids=set(engine.containers)
    operation(controller,'switch default_chain')
    assert not old_ids & set(engine.containers)
    removed=[i for i,e in enumerate(engine.events) if e[0]=='rm' and e[1] in old_ids]
    created=[i for i,e in enumerate(engine.events) if e[0]=='create' and e[1] in engine.containers]
    assert max(removed)<min(created)


def test_anchor_change_stops_old_group_and_blocks_new_start(runtime_controller):
    from robot_control.runtime_manager import BlockedInfrastructure
    controller,engine,_=ready(runtime_controller)
    def broken(deadline, **kwargs):raise BlockedInfrastructure('anchor changed')
    controller.runtime.check_infrastructure=broken
    operation(controller,'switch second','failed')
    assert controller.status()['runtime']['phase']=='BLOCKED_INFRA'
    assert not engine.containers
    assert controller.status()['policy'] is None


def test_failed_startup_recovery_blocks_manual_switch(runtime_controller):
    controller,engine,_=runtime_controller
    def unavailable(*args,**kwargs):raise RuntimeError('external reader unavailable')
    controller._external.recover=unavailable
    controller.start()
    until(lambda:controller.status()['state']=='fault')
    assert controller.status()['runtime']['phase']=='BLOCKED_CLEANUP'
    operation(controller,'switch second','failed')
    assert not engine.containers
