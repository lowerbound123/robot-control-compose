"""Optional diagnostics must expose timings without gating control work."""
import importlib.util
import threading
import time


def test_trace_is_optional_bounded_and_contains_only_diagnostic_scalars():
    assert importlib.util.find_spec('rc_policy_sdk.performance'), 'performance trace helper is missing'
    from rc_policy_sdk.performance import Trace, trace
    assert trace({}) is None
    item = Trace.start('run:request:12')
    item.mark('policy.cache', remaining_actions=29, secret={'token': 'must-not-copy'})
    for _ in range(300):
        item.mark('stage')
    assert item.data['trace_id'] == 'run:request:12'
    assert 0 < len(item.data['points']) <= 128
    assert item.data['dropped_points'] > 0
    assert item.data['points'][0]['remaining_actions'] == 29
    assert 'secret' not in item.data['points'][0]
    assert all(p['monotonic_ns'] > 0 for p in item.data['points'])
    assert trace({'_performance': {'version': 999}}) is None


def test_info_has_no_diagnostic_output_or_trace_and_debug_is_asynchronous():
    assert importlib.util.find_spec('robot_control.performance'), 'performance log sink is missing'
    from robot_control.performance import PerformanceLogger
    lines=[]
    disabled=PerformanceLogger('INFO','run',lines.append)
    assert disabled.trace('request') is None
    disabled.emit('stage',request_id=1)
    assert disabled.close(time.monotonic()+1)
    assert not lines
    entered,release=threading.Event(),threading.Event()
    def slow_sink(row):
        entered.set()
        assert release.wait(2)
        lines.append(row)
    enabled=PerformanceLogger('DEBUG','run',slow_sink,capacity=2)
    try:
        enabled.emit('first',request_id=1)
        assert entered.wait(1)
        finished=threading.Event()
        def produce():
            for i in range(20):enabled.emit('later',request_id=i)
            finished.set()
        producer=threading.Thread(target=produce);producer.start()
        assert finished.wait(.2), 'diagnostics blocked their producer on the sink'
        release.set();producer.join(1)
    finally:
        release.set()
        assert enabled.close(time.monotonic()+2)
    assert all(row['level']=='debug' and row['event']=='performance' for row in lines)
    assert any(row.get('dropped_events',0)>0 for row in lines)
    assert all(row['rc_run_id']=='run' for row in lines)


def test_recorder_profiles_lock_and_copy_without_recursive_debug_events(tmp_path):
    from robot_control.recording import RecordingService
    from robot_control.performance import PerformanceLogger
    lines=[]
    recorder=RecordingService(tmp_path,'run',lambda:None)
    recorder._state,recorder._session='recording','session'
    logger=PerformanceLogger('DEBUG','run',lines.append)
    recorder.performance=logger
    try:
        assert recorder.event('policy',{'event':'policy.result','request_id':7})
        assert recorder.log({'event':'performance','message':'performance test'})
    finally:
        assert logger.close(time.monotonic()+2)
    assert len(lines)==1
    assert lines[0]['stage']=='record.enqueue' and lines[0]['request_id']==7
    assert lines[0]['lock_wait_ns']>=0 and lines[0]['copy_ns']>=0


def test_diagnostic_sink_failure_cannot_reject_or_hide_policy_result(tmp_path):
    from rc_policy_sdk.performance import Trace
    from test_recording_hooks import remote_worker, inference_request, receive_result
    worker=remote_worker(tmp_path,None)
    def broken(*args,**kwargs):raise RuntimeError('diagnostic sink failed')
    worker.on_performance=broken
    request=inference_request()
    perf=Trace.start('run:failure-isolation')
    perf.attach(request)
    worker.submit(request)
    result=receive_result(worker,request,_performance=perf.snapshot())
    assert worker.poll()==result


def test_performance_envelope_does_not_strip_same_named_business_state(tmp_path):
    from rc_policy_sdk.performance import Trace
    from test_recording_hooks import remote_worker, inference_request
    events=[]
    worker=remote_worker(tmp_path,events.append)
    request=inference_request()
    request['state']['_performance']={'business':'preserve this original state field'}
    Trace.start('run:envelope').attach(request)
    worker.submit(request)
    assert events[0]['data']['state']==request['state']
    assert '_performance' not in events[0]['data']
