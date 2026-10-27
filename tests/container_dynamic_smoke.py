"""Runs inside the validation RC container, using the real Docker daemon and SDK."""
import json
import os
from pathlib import Path
import time

from robot_control.controller import RobotController
from robot_control.runtime_manager import ComposeRuntimeManager
from robot_control.config import PolicyRegistry

ROOT=Path('/workspace')
TRACE=[]


def wait(predicate,timeout=45):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if predicate():return
        time.sleep(.02)
    raise AssertionError('condition timed out')


def command(controller,line,expected='succeeded'):
    receipt=controller.submit(line)
    wait(lambda:controller.get_operation(receipt)['status'] in ('succeeded','failed','cancelled'))
    result=controller.get_operation(receipt)
    assert result['status']==expected,result
    return result.get('result')


def make():
    controller=RobotController(ROOT/'configs/rc.mock.json')
    original=controller.runtime.runner
    def runner(args,**kwargs):
        result=original(args,**kwargs)
        if args[0] in ('start','stop','kill','rm') or args[0]=='compose' and 'create' in args:
            TRACE.append({'time':time.time(),'command':args,'result':result.strip()})
        return result
    controller.runtime.runner=runner
    controller.start()
    try:
        wait(lambda:controller._connected and controller._operation is None)
    except Exception:
        print(controller.read_events(),flush=True)
        controller.submit('exit');controller.done.wait(25)
        raise
    return controller


def ids(controller):
    return controller.runtime._ids({'io.robot-control.deployment':controller.runtime.context['deployment_id']},time.monotonic()+10)


def run():
    outcomes=[]
    controller=make()
    try:
        assert controller.status()['policy'] is None and not ids(controller)
        base=controller.runtime._ids({'com.docker.compose.project':controller.runtime.context['base_project']},time.monotonic()+10)
        assert len(base)==2
        command(controller,'help a')
        command(controller,'policy a info','failed')
        assert not ids(controller)
        outcomes.append('base exactly two; EMPTY; static help creates nothing')
        command(controller,'switch a')
        first=ids(controller)[0]
        first_instance=controller._worker.host.instance_id
        info=command(controller,'policy a info')
        assert info['python']==[3,14] and info['external']=='shared'
        command(controller,'resume')
        wait(lambda:controller.buffer.consumed>2)
        info=command(controller,'policy a info')
        assert info['frames']>0
        command(controller,'switch a')
        assert first not in ids(controller)
        assert controller._worker.host.instance_id!=first_instance
        assert controller.status()['state']=='paused'
        assert all(abs(x)<1e-5 for x in controller.robot.read_state()['qpos'])
        outcomes.append('Python3.10 to 3.14; read-only shared images; same-ID cold recreate; no implicit reset')
        command(controller,'switch pair')
        old=set(ids(controller));assert len(old)==2
        members=controller._worker.monitor_members()
        assert members['a'].host.instance_id!=members['b'].host.instance_id
        images={i['Image'] for i in controller.runtime._inspect(list(old),time.monotonic()+10)}
        assert len(images)==1
        command(controller,'resume')
        wait(lambda:controller.buffer.consumed>2)
        assert command(controller,'policy a info')['frames']>0
        command(controller,'info','failed')
        boundary=len(TRACE)
        command(controller,'switch next')
        new=set(ids(controller));assert len(new)==2 and not old&new
        events=TRACE[boundary:]
        removed=[i for i,e in enumerate(events) if e['command'][0]=='rm' and e['command'][-1] in old]
        created=[i for i,e in enumerate(events) if e['command'][0]=='compose' and 'create' in e['command']]
        assert len(removed)==2 and max(removed)<min(created)
        outcomes.append('chain a,b,a deduplicates; a,b to b,c destroys all old CIDs before create; explicit commands')
        (ROOT/'policy/b/compose.yaml').unlink()
        command(controller,'switch a')
        assert not new&set(ids(controller))
        outcomes.append('deleted old Compose still cleans by CID/labels')
        command(controller,'switch failed','failed')
        assert controller.status()['policy'] is None and not ids(controller)
        outcomes.append('initialize failure leaves FAULT_EMPTY with no orphan')
        command(controller,'switch partial','failed')
        assert controller.status()['policy'] is None and not ids(controller)
        outcomes.append('chain partial initialize failure reclaims every created member')
        command(controller,'switch slow')
        before=time.monotonic()
        command(controller,'switch a')
        assert time.monotonic()-before<20
        assert controller.exit_code==1 and controller.status()['runtime']['cleanup_forced']
        outcomes.append('hanging release forces bounded cleanup; paused successor; nonzero fact retained')
        command(controller,'switch a --reset')
        assert all(abs(x-.25)<1e-5 for x in controller.robot.read_state()['qpos'])
        outcomes.append('explicit --reset moves only through control thread')
        controller.submit('exit');assert controller.done.wait(25)
        assert not ids(controller)
        # Simulate a previous RC's durable intent/container without any host session.
        runtime=ComposeRuntimeManager.from_file(ROOT/'.setup/runtime.json',run_id='abandoned')
        deadline=time.monotonic()+20;runtime.acquire(deadline);runtime.check_infrastructure(deadline)
        record=PolicyRegistry(ROOT/'policy').get('a')
        handle=runtime.start(runtime.prepare_spec(record,deadline=deadline),1,__import__('threading').Event(),deadline)
        # A crashed RC's non-external image slots also survive while anchor lives.
        import posix_ipc, mmap, uuid
        from rc_policy_sdk.images import _HEADER
        old_pool=uuid.uuid4().hex[:12]
        runtime.record_pool(old_pool)
        name='/rc'+old_pool+'0000000001'
        segment=posix_ipc.SharedMemory(name,flags=posix_ipc.O_CREX,size=_HEADER.size+4)
        with mmap.mmap(segment.fd,_HEADER.size+4) as mapping:
            mapping[:]=_HEADER.pack(4,1)+b'data'
        segment.close_fd()
        runtime.release()
        controller=make()
        try:
            leftover=posix_ipc.SharedMemory(name);leftover.close_fd()
            raise AssertionError('old RC shared image pool survived recovery')
        except posix_ipc.ExistentialError:
            pass
        assert handle.container_id not in ids(controller)
        outcomes.append('RC new run reconciles and removes previous run container from journal')
        command(controller,'switch a')
        old_anchor=controller.runtime.anchor_id
        deadline=time.monotonic()+20
        controller.runtime._run(['stop','--time','2',old_anchor],deadline)
        # An additional anchor with the same service labels makes infrastructure
        # ambiguous; RC must close its old policy and refuse to create a new one.
        context=controller.runtime.context
        controller.runtime._run(['run','-d','--ipc','shareable',
            '--label','com.docker.compose.project='+context['base_project'],
            '--label','com.docker.compose.service=ipc-anchor',
            '--label','com.docker.compose.config-hash=validation',
            '--label','com.docker.compose.oneoff=False',
            '--label','com.docker.compose.container-number=2',
            '--env','RC_UID='+str(context['uid']),'--env','RC_GID='+str(context['gid']),
            '--volume',context['rpc_volume']+':/run/robot-control','robot-control-compose-anchor:local'],deadline)
        command(controller,'switch c','failed')
        assert controller.status()['runtime']['phase']=='BLOCKED_INFRA' and not ids(controller)
        assert controller.status()['policy'] is None
        outcomes.append('anchor replacement/ambiguity blocks new group and closes old group')
    finally:
        if not controller.done.is_set():
            controller.submit('exit')
            assert controller.done.wait(25),controller.status()
        (ROOT/'outputs/smoke.json').write_text(json.dumps({'outcomes':outcomes,'trace':TRACE},indent=2))
    print(json.dumps({'passed':len(outcomes),'outcomes':outcomes}),flush=True)
    return 1  # The accepted hanging-release scenario must retain a nonzero process fact.


if __name__=='__main__':
    raise SystemExit(run())
