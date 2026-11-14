"""Deterministic auto-record intent and asynchronous rotation boundaries."""
from concurrent.futures import Future
import threading
from types import SimpleNamespace

import pytest

from robot_control.operations import Operations
from robot_control.recording_control import RecordingControl


class Recorder:
    def __init__(self,*args,**kwargs):
        self.state='idle';self.number=0;self.hold_stop=False;self.pending=[]
        self.starts=[];self.events=[];self.stop_timeout=2
    @property
    def active(self): return self.state=='recording'
    def status(self):
        return dict(state=self.state,session_id=str(self.number) if self.number else None,path=str(self.number),
            error=None,samples_written=0,repeated_frames=0,missing_frames=0,missed_samples=0,pending_bytes=0)
    def command(self,action,data=None):
        result=Future()
        if action=='on':
            if self.state=='stopping':
                result.set_exception(RuntimeError('recording is stopping'));return result
            if not self.active:
                self.number+=1;self.starts.append(data)
            self.state='recording'
        elif action=='off':
            self.state='stopping'
            if self.hold_stop:
                self.pending.append(result);return result
            self.state='idle'
        else: raise AssertionError(action)
        result.set_result(self.status());return result
    def release(self,error=None):
        self.state='error' if error else 'idle'
        for future in self.pending:
            if error: future.set_exception(RuntimeError(error))
            else: future.set_result(self.status())
        self.pending.clear()
    def event(self,*args,**kwargs): self.events.append(args);return True


class Control(RecordingControl):
    def __init__(self,tmp_path):
        self._lock=threading.RLock();self._stop=threading.Event()
        self.outputs=tmp_path;self.run_id='auto-test';self.config={};self.robot=SimpleNamespace()
        self.operations=Operations(control_lock=self._lock);self._connected=True
        self._connector_record_transition=False;self._connector_retry_available=False
        self._connector_record_wanted=False;self.exit_code=0;self.logs=[]
        self._init_recording()
    def _record_metadata(self): return {'policy':'current-policy'}
    def _emit(self,*args,**kwargs): self.logs.append(args)
    def _emit_command(self,*args,**kwargs): self.logs.append(args)
    def _ensure_record_auto_worker(self): pass  # Tests advance the real state machine explicitly.
    def command(self,*args):
        identity=self.operations.create('record '+' '.join(args))
        with self._lock: self._submit_recording(identity,['record',*args])
        return self.operations.get(identity)
    def pump(self,count=8):
        for _ in range(count):
            with self._lock: self._record_auto_step()
    def begin(self,reason='reset'):
        identity=self.operations.create('operation')
        with self._lock: self._record_auto_mark(identity,reason)
        return identity
    def finish(self,identity,status='succeeded'):
        self.operations.update(identity,status,result={'actual_action':'complete'})


@pytest.fixture
def control(tmp_path,monkeypatch):
    import robot_control.recording_control as module
    monkeypatch.setattr(module,'RecordingService',Recorder)
    return Control(tmp_path)


def test_auto_commands_and_manual_stop_preserve_independent_mode(control):
    c=control
    assert c._record_status()['auto'] is False
    assert c.command('auto','on')['status']=='succeeded'
    first=c._record_status()['session_id']
    assert c.command('auto','on')['result']['session_id']==first
    assert c.command('auto','off')['result']['auto'] is False
    assert c._recorder.active and c._recorder.number==1
    c.command('auto')
    c.command('off')
    assert c._record_status()['auto'] and not c._recorder.active
    c.finish(c.begin());c.pump()
    assert c._recorder.active and c._recorder.number==2


def test_success_is_immediate_rotation_is_serial_and_has_provenance(control):
    c=control;c.command('auto','on');c._recorder.hold_stop=True
    first=c.begin();c.finish(first);c.pump()
    assert c.operations.get(first)['status']=='succeeded'
    assert c._record_status()['auto_pending']==1 and c._recorder.state=='stopping'
    second=c.begin('policy.switch');c.finish(second)
    assert c._record_status()['auto_pending']==2
    c._recorder.hold_stop=False;c._recorder.release();c.pump()
    assert c._recorder.number==3 and c._record_status()['auto_pending']==0
    assert c._recorder.starts[1]['record_auto']==dict(operation_id=first,reason='reset',previous_session_id='1')
    assert c._recorder.starts[2]['record_auto']['operation_id']==second
    assert c._recorder.starts[2]['policy']=='current-policy'


@pytest.mark.parametrize('terminal',['failed','cancelled'])
def test_failed_or_cancelled_operation_does_not_rotate(control,terminal):
    c=control;c.command('auto','on');c.finish(c.begin(),terminal);c.pump()
    assert c._recorder.number==1


def test_manual_stop_invalidates_inflight_operation_and_pending_reopen(control):
    c=control;c.command('auto','on')
    late=c.begin()
    c._recorder.hold_stop=True
    c.finish(c.begin());c.pump()
    c.command('off')
    c.finish(late)
    c._recorder.release();c.pump()
    assert c._record_status()['auto'] and c._record_status()['auto_pending']==0
    assert c._recorder.number==1 and not c._recorder.active
    c._recorder.hold_stop=False;c.finish(c.begin());c.pump()
    assert c._recorder.number==2 and c._recorder.active


def test_disable_cancels_queued_rotations_but_restores_already_stopping_session(control):
    c=control;c.command('auto','on');c._recorder.hold_stop=True
    c.finish(c.begin());c.pump()
    c.finish(c.begin());c.command('auto','off')
    c._recorder.release();c.pump()
    assert not c._record_status()['auto'] and c._recorder.active and c._recorder.number==2
    assert c._record_status()['auto_pending']==0


def test_error_does_not_rewrite_success_or_retry_forever(control):
    c=control;c.command('auto','on');c._recorder.hold_stop=True
    identity=c.begin();c.finish(identity);c.pump()
    c._recorder.release('disk failure');c.pump()
    assert c.operations.get(identity)['status']=='succeeded'
    assert c._record_status()['auto'] and 'disk failure' in c._record_status()['auto_error']
    assert c._record_status()['auto_pending']==0 and c._recorder.number==1


def test_connector_boundary_already_handled_does_not_double_rotate(control):
    c=control;c.command('auto','on');identity=c.begin('connector.switch')
    c._record_auto_handled(identity);c.finish(identity);c.pump()
    assert c._recorder.number==1


def test_queue_is_bounded_and_overflow_is_visible(control):
    c=control;c.command('auto','on')
    for _ in range(257): c.finish(c.begin())
    assert c._record_status()['auto_pending']==256
    assert 'capacity' in c._record_status()['auto_error']


def test_shutdown_prevents_late_reopen(control):
    c=control;c.command('auto','on');c._recorder.hold_stop=True
    c.finish(c.begin());c.pump();c._stop.set()
    c._recorder.release();c.pump()
    assert c._recorder.number==1 and not c._recorder.active


@pytest.mark.parametrize('text',['record auto','record auto on','record auto off'])
def test_public_auto_syntax_normalizes_without_legacy_aliases(text):
    from rc_policy_sdk.command_syntax import parse_command
    from robot_control.human_commands import execution_line
    parsed=parse_command(text)
    assert parsed.kind=='record' and not parsed.readonly
    assert execution_line(parsed)==text


from test_controller import setup_controller,ready,until
from test_recording_controller import operation


def test_controller_auto_starts_rotates_after_success_and_keeps_metadata(setup_controller):
    import json
    from pathlib import Path
    c,_,_=setup_controller;ready(c)
    assert c.query('record status')['auto'] is False
    first=operation(c,'record auto on')
    assert c._statusbar_snapshot()['recording_auto'] is True
    until(lambda:c.status()['recording']['samples_written']>=2)
    identity=c.submit('reset')
    until(lambda:c.get_operation(identity)['status']=='succeeded')
    until(lambda:c.status()['recording']['session_id']!=first['session_id'] and c.status()['recording']['auto_pending']==0)
    second=c.query('record status')
    metadata=json.loads((Path(second['path'])/'configs/initial.json').read_text())
    assert metadata['record_auto']==dict(operation_id=identity,reason='reset',previous_session_id=first['session_id'])
    assert (Path(first['path'])/'video_segments.jsonl').exists()
    stopped=operation(c,'record stop');assert stopped['auto'] and stopped['state']=='idle'
    operation(c,'reset')
    until(lambda:c.status()['recording']['state']=='recording')
    assert c.status()['recording']['session_id']!=second['session_id']
    current=c.status()['recording']['session_id']
    operation(c,'record auto off')
    operation(c,'reset')
    assert c.status()['recording']['session_id']==current


@pytest.mark.parametrize('command',['policy switch second','policy switch second --reset','pose restore auto_pose'])
def test_controller_semantic_success_cuts_once(setup_controller,command):
    import json
    from pathlib import Path
    c,_,_=setup_controller;ready(c)
    operation(c,'pose save auto_pose')
    first=operation(c,'record auto on')
    operation(c,command)
    until(lambda:c.status()['recording']['session_id']!=first['session_id'] and c.status()['recording']['auto_pending']==0)
    assert len(list((c.outputs/'logs').glob('record_*')))==2
    metadata=json.loads((Path(c.status()['recording']['path'])/'configs/initial.json').read_text())
    if command.startswith('policy'): assert metadata['policy']=='second'


def test_resume_return_and_failed_switch_do_not_cut(setup_controller):
    c,_,_=setup_controller;worker=ready(c)
    first=operation(c,'record auto on')
    operation(c,'resume');operation(c,'pause')
    if c._inflight is not None: worker.finish([[.1]*14])
    until(lambda:c._inflight is None)
    operation(c,'take');operation(c,'return');operation(c,'pause')
    if c._inflight is not None: worker.finish([[.1]*14])
    until(lambda:c._inflight is None)
    identity=c.submit('policy switch missing-policy')
    until(lambda:c.get_operation(identity)['status']=='failed')
    assert c.status()['recording']['session_id']==first['session_id']
    assert c.status()['recording']['auto_pending']==0


def test_reset_receipt_does_not_wait_for_recorder_stop(setup_controller,monkeypatch):
    c,_,_=setup_controller;ready(c);operation(c,'record auto on')
    original=c._recorder.command;gate=Future();entered=threading.Event()
    def delayed(action,*args,**kwargs):
        if action=='off': entered.set();return gate
        return original(action,*args,**kwargs)
    monkeypatch.setattr(c._recorder,'command',delayed)
    try:
        identity=c.submit('reset')
        until(lambda:c.get_operation(identity)['status']=='succeeded')
        assert entered.wait(2) and not gate.done()
        operation(c,'policy switch second')
        assert c.status()['recording']['auto_pending']==2
    finally:
        monkeypatch.setattr(c._recorder,'command',original)
        gate.set_exception(RuntimeError('injected stop failure'))
        until(lambda:c.status()['recording']['auto_pending']==0)
        # The failure remains independent of the already-successful reset.
        assert c.get_operation(identity)['status']=='succeeded'
        assert c.status()['recording']['auto']
        c.exit_code=0  # Fixture cleanup need not reinterpret this injected error.


def test_auto_enable_failure_is_visible_and_does_not_disable_mode(control,monkeypatch):
    def failed(*args,**kwargs):
        future=Future();future.set_exception(RuntimeError('ffmpeg unavailable'));return future
    monkeypatch.setattr(control._recorder,'command',failed)
    assert control.command('auto','on')['status']=='failed'
    assert control._record_status()['auto']
    assert 'ffmpeg unavailable' in control._record_status()['auto_error']


def test_auto_worker_is_reaped_when_unstarted_controller_is_closed(setup_controller):
    c,_,_=setup_controller
    identity=c.submit('record auto on')
    assert c.get_operation(identity)['status']=='failed'
    c.close_unstarted()
    assert c._record_auto_thread is not None and not c._record_auto_thread.is_alive()


def test_enable_during_stop_waits_and_manual_stop_can_supersede(control):
    c=control;c.command('on');c._recorder.hold_stop=True;c.command('off')
    enabled=c.command('auto','on')
    assert enabled['status']=='running'
    c._recorder.release();c.pump()
    assert c.operations.get(enabled['operation_id'])['status']=='succeeded'
    assert c._recorder.active and c._recorder.number==2
    c.command('off')
    enabled=c.command('auto','on')
    c.command('off');c._recorder.release();c.pump()
    assert c.operations.get(enabled['operation_id'])['status']=='cancelled'
    assert not c._recorder.active and c._record_auto


def test_enable_during_auto_rotation_reuses_its_new_session(control):
    c=control;c.command('auto','on');c._recorder.hold_stop=True
    trigger=c.begin();c.finish(trigger);c.pump()
    enabled=c.command('auto','on')
    assert enabled['status']=='running'
    c._recorder.release();c.pump()
    assert c.operations.get(enabled['operation_id'])['status']=='succeeded'
    assert c._recorder.number==2 and c._recorder.starts[-1]['record_auto']['operation_id']==trigger


def test_disabling_auto_preserves_explicit_pending_enable(control):
    c=control;c.command('on');c._recorder.hold_stop=True;c.command('off')
    enabled=c.command('auto','on');assert enabled['status']=='running'
    c.command('auto','off');c._recorder.release();c.pump()
    assert c.operations.get(enabled['operation_id'])['status']=='succeeded'
    assert not c._record_auto and c._recorder.active


def test_shutdown_settles_pending_enable_without_reopening(control):
    import time
    c=control;c.command('on');c._recorder.hold_stop=True;c.command('off')
    enabled=c.command('auto','on')
    c._stop.set();c._close_record_auto(time.monotonic()+1)
    c._recorder.release();c.pump()
    assert c.operations.get(enabled['operation_id'])['status']=='cancelled'
    assert not c._recorder.active and c._record_status()['auto_pending']==0


def test_plain_record_uses_actual_state_after_recording_failure(control):
    c=control;c.command('on');c._recorder.state='error'
    reply=c.command()
    assert reply['status']=='succeeded' and c._recorder.active and c._recorder.number==2


@pytest.mark.parametrize('failed',[False,True])
def test_reduce_receipt_contains_auto_status_fields(control,failed):
    c=control;c.command('auto','on')
    identity=c.operations.create('record reduce all')
    from robot_control.recording_reduce import ReductionFailed
    result=Future();summary=dict(target='all',converted=0,skipped=0,failed=int(failed))
    if failed: result.set_exception(ReductionFailed('conversion failed',summary))
    else: result.set_result(summary)
    c._reduction_finished(identity,result)
    reply=c.operations.get(identity)['result']
    assert reply['auto'] is True and reply['auto_pending']==0 and reply['auto_error'] is None


def test_auto_start_defers_until_failed_connector_is_ready(control):
    c=control;c.command('auto','on');c.command('off')
    c._connected=False;c._connector_retry_available=True
    c.finish(c.begin());c.pump()
    assert c._recorder.number==1 and c._record_status()['auto_pending']==1
    c._connected=True;c._connector_retry_available=False;c.pump()
    assert c._recorder.number==2 and c._recorder.active


def test_older_rotation_success_does_not_clear_newer_auto_error(control):
    c=control;c.command('auto','on');c._recorder.hold_stop=True
    c.finish(c.begin());c.pump()
    c._record_auto_failed('later queue capacity error')
    c._recorder.release();c.pump()
    assert c._record_status()['auto_error']=='later queue capacity error'
    c._recorder.hold_stop=False;c.finish(c.begin());c.pump()
    assert c._record_status()['auto_error'] is None


def test_exit_during_auto_drain_reaps_thread_and_ignores_late_callback(setup_controller,monkeypatch):
    from pathlib import Path
    c,_,_=setup_controller;ready(c);operation(c,'record auto on')
    gate=Future();entered=threading.Event();original=c._recorder.command
    underlying=[]
    def delayed(action,*args,**kwargs):
        result=original(action,*args,**kwargs)
        if action=='off':
            underlying.append(result);entered.set();return gate
        return result
    monkeypatch.setattr(c._recorder,'command',delayed)
    operation(c,'reset');assert entered.wait(2)
    c.submit('exit');assert c.done.wait(3)
    assert c.exit_code==0 and not c._record_auto_thread.is_alive()
    gate.set_result(underlying[0].result(1))
    assert len(list((c.outputs/'logs').glob('record_*')))==1
    assert c.status()['recording']['auto_pending']==0
