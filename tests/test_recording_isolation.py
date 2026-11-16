"""A stalled recording backend must not gate control or result availability."""
import json
from concurrent.futures import Future
from pathlib import Path
import threading
import time

import pytest

from robot_control.recording import RecordingService
from robot_control.performance import PerformanceLogger
from test_recording_hooks import remote_worker, inference_request, receive_result
from test_recording_faults import recorder


@pytest.fixture
def service(tmp_path):
    recorder = RecordingService(tmp_path, 'isolation',
        lambda: {'state': {}, 'state_error': None, 'frames': {}})
    recorder.start()
    recorder.command('on').result(3)
    yield recorder
    recorder.shutdown(time.monotonic() + 3)


def run_before_unlock(lock, function):
    done, errors, result = threading.Event(), [], []
    def run():
        try: result.append(function())
        except BaseException as exc: errors.append(exc)
        finally: done.set()
    with lock:
        thread = threading.Thread(target=run)
        thread.start()
        completed = done.wait(.3)
    thread.join(3)
    assert completed, 'producer waited for the recording backend lock'
    assert not errors, errors
    return result[0]


@pytest.mark.parametrize('operation', ['attempt', 'publication', 'event', 'log', 'status', 'active', 'command'])
def test_producers_and_status_do_not_wait_for_backend_lock(service, operation):
    operations = {
        'attempt': lambda: service.control_attempt({'requested': [1.] * 14}),
        'publication': lambda: service.sent({'arm': 'left', 'mode': 'qpos', 'values7': [1.] * 7,
            'timestamp_ns': time.time_ns(), 'monotonic_ns': time.monotonic_ns()}),
        'event': lambda: service.event('policy', {'event': 'policy.result', 'request_id': 7}),
        'log': lambda: service.log({'message': 'control diagnostic'}),
        'status': service.status,
        'active': lambda: service.active,
        'command': lambda: service.command('off'),
    }
    result = run_before_unlock(service._lock, operations[operation])
    if operation == 'command': assert result.result(3)['state'] == 'idle'
    if operation == 'status': assert result['state'] == 'recording'
    if operation == 'active': assert result is True


def test_policy_result_available_while_recording_backend_is_locked(service, tmp_path):
    worker = remote_worker(tmp_path, service.policy_event)
    request = inference_request()
    worker.submit(request)
    def receive_and_poll():
        response = receive_result(worker, request)
        assert worker.poll() == response
        return response
    assert run_before_unlock(service._lock, receive_and_poll)['type'] == 'result'


def test_slow_metadata_snapshot_does_not_hold_recording_admission(service):
    entered, release = threading.Event(), threading.Event()
    class SlowSnapshot:
        def __deepcopy__(self, memo):
            entered.set()
            assert release.wait(3)
            return {'value': 7}
    producer = threading.Thread(target=lambda: service.event('policy', {'slow': SlowSnapshot()}))
    producer.start()
    try:
        assert entered.wait(1)
        finished = threading.Event()
        def attempt():
            service.control_attempt({'requested': [2.] * 14})
            finished.set()
        control = threading.Thread(target=attempt); control.start()
        completed = finished.wait(.3)
    finally:
        release.set(); producer.join(3); control.join(3)
    assert completed, 'metadata copying held the recording admission lock'


def rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def test_ingress_snapshot_and_debug_are_drained_in_original_session(service):
    logger = PerformanceLogger('DEBUG', 'isolation', ingress=service)
    data = {'event': 'queued.result', 'actions': [[7.] * 14]}
    with service._lock:
        assert service.event('policy', data)
        data['actions'][0][0] = 99.
        logger.emit('before-stop', request_id=17)
        stopping = service.command('off')
        assert not stopping.done()
        assert service.status()['ingress_pending'] >= 2
    first = stopping.result(3)
    assert service.status()['pending_bytes'] == 0
    logger.emit('between-sessions', request_id=18)
    second = service.command('on').result(3)
    logger.emit('second-session', request_id=19)
    service.command('off').result(3)
    assert logger.close(time.monotonic()+1)
    saved = next(row for row in rows(Path(first['path'])/'policy.jsonl') if row['event'] == 'queued.result')
    assert saved['actions'] == [[7.] * 14]
    first_debug = [row['stage'] for row in rows(Path(first['path'])/'events.jsonl') if row.get('event') == 'performance']
    second_debug = [row['stage'] for row in rows(Path(second['path'])/'events.jsonl') if row.get('event') == 'performance']
    assert first_debug == ['before-stop']
    assert second_debug == ['second-session']


def test_debug_saturation_is_bounded_and_cannot_gate_publication(service):
    logger = PerformanceLogger('DEBUG', 'isolation', ingress=service)
    def flood():
        for number in range(2100): logger.emit('flood', request_id=number)
        service.sent({'arm': 'left', 'mode': 'qpos', 'values7': [3.] * 7,
                     'timestamp_ns':time.time_ns(), 'monotonic_ns':time.monotonic_ns()})
        return service.status()
    status = run_before_unlock(service._lock, flood)
    assert status['debug_dropped'] == 52
    assert 0 < status['pending_bytes'] < service.sample_budget+service.extra_budget
    stopped = service.command('off').result(3)
    gaps = [row for row in rows(Path(stopped['path'])/'session.jsonl') if row['event'] == 'diagnostic_gap']
    assert sum(row.get('reasons', {}).get('debug_capacity', 0) for row in gaps) == 52
    assert any(row['event'] == 'control.published' for row in rows(Path(stopped['path'])/'control.jsonl'))
    logger.close(time.monotonic()+1)


def test_drop_during_stop_fence_does_not_move_old_losses_to_base_log(service, monkeypatch):
    oversized = 'x' * (9 * 1024 * 1024)
    flush = service._flush_gap
    injected = []
    def at_fence(**options):
        if service.status()['state'] == 'stopping' and not injected:
            injected.append(True)
            assert not service.log({'message': oversized})
        return flush(**options)
    monkeypatch.setattr(service, '_flush_gap', at_fence)
    completion = Future()
    with service._lock:
        assert not service.event('policy', {'payload': oversized})
        service._request('off', {}, completion)
    stopped = completion.result(3)
    gaps = [row for row in rows(Path(stopped['path'])/'session.jsonl') if row['event'] == 'diagnostic_gap']
    assert sum(row['count'] for row in gaps) == 1
    service.shutdown(time.monotonic()+3)
    base = [row for row in rows(service.directory/'rc_isolation.jsonl') if row.get('event') == 'diagnostic_gap']
    assert sum(row['count'] for row in base if row['record_session_id'] is None) == 1


def test_frame_metadata_is_owned_at_admission_without_copying_pixels(service):
    import numpy as np
    pixels = np.full((2, 3, 3), 7, np.uint8)
    pixels.flags.writeable = False
    frames = {'head': {'image': pixels, 'timestamp': 1., 'valid': True}}
    with service._lock:
        assert service.event('policy', {'event': 'frame.snapshot'}, frames=frames)
        frames['head']['timestamp'] = 99.
    stopped = service.command('off').result(3)
    event = next(row for row in rows(Path(stopped['path'])/'policy.jsonl') if row['event'] == 'frame.snapshot')
    assert event['image_files']['head']['timestamp'] == 1.


def test_debug_format_failure_drops_diagnostic_without_failing_recording(service, monkeypatch):
    from robot_control import recording
    logger = PerformanceLogger('DEBUG', 'isolation', ingress=service)
    def broken(value): raise ValueError('injected diagnostic formatter error')
    monkeypatch.setattr(recording, 'format_log_time', broken)
    logger.emit('format-failure')
    deadline = time.monotonic()+2
    while service.status()['ingress_pending'] and time.monotonic() < deadline: time.sleep(.005)
    assert service.status()['state'] == 'recording'
    assert service.status()['debug_dropped'] == 1
    stopped = service.command('off').result(3)
    assert stopped['error'] is None
    logger.close(time.monotonic()+1)


def test_cancelled_command_futures_do_not_bypass_pending_command_limit(service):
    with service._lock:
        for _ in range(256):
            pending = service.command('on')
            assert pending.cancel()
        rejected = service.command('on')
        assert rejected.done(), 'cancelled queued commands bypassed the count limit'
        with pytest.raises(RuntimeError, match='full'): rejected.result()


def test_start_metadata_credit_remains_reserved_during_backend_preparation(service, monkeypatch):
    service.command('off').result(3)
    entered, release = threading.Event(), threading.Event()
    drain = service._drain_ingress
    def gated(**options):
        if options.get('all_entries') and service.status()['state'] == 'idle':
            entered.set()
            assert release.wait(3)
        return drain(**options)
    monkeypatch.setattr(service, '_drain_ingress', gated)
    starting = service.command('on', {'padding': 'x'*100000})
    try:
        assert entered.wait(1)
        assert service.status()['pending_bytes'] >= 800000
    finally:
        release.set()
    assert starting.result(3)['state'] == 'recording'


def test_performance_records_do_not_profile_their_own_background_preparation(service):
    emitted = []
    logger = PerformanceLogger('DEBUG', 'isolation', emitted.append)
    service.performance = logger
    try:
        assert service.log({'event': 'performance', 'message': 'external diagnostic'})
        service.command('off').result(3)
    finally:
        logger.close(time.monotonic()+2)
        service.performance = None
    assert not [row for row in emitted if row['stage'] == 'record.prepare' and row.get('event_name') == 'performance']


def test_log_snapshot_failure_is_counted_without_propagating_to_control(service):
    class BrokenSnapshot:
        def __deepcopy__(self, memo): raise ValueError('injected log snapshot failure')
    assert service.log({'message': 'test', 'details': BrokenSnapshot()}) is False
    assert service.status()['state'] == 'recording'
    stopped = service.command('off').result(3)
    assert stopped['error'] is None
    gaps = [row for row in rows(Path(stopped['path'])/'session.jsonl') if row['event'] == 'diagnostic_gap']
    assert sum(row.get('reasons', {}).get('snapshot_failed', 0) for row in gaps) == 1


def test_publication_history_overflow_fails_recording_without_evicting_or_raising(recorder):
    item = recorder()
    service = item.service
    service.command('on').result(3)
    assert item.entered.wait(1)  # Sampler cannot consume the published history.
    for number in range(4097):
        service.sent({'arm': 'left', 'mode': 'qpos', 'values7': [float(number)] * 7,
            'attempt_id': f'attempt-{number}', 'timestamp_ns':time.time_ns(),
            'monotonic_ns':time.monotonic_ns()})
    status = service.status()
    assert status['state'] == 'error' and 'history overflow' in status['error']
    assert len(service._sent) == 4096
    assert service._sent[0]['attempt_id'] == 'attempt-0'
    assert service._sent[-1]['attempt_id'] == 'attempt-4095'
    # Motion code still gets its attempt identity after recording has failed.
    assert service.control_attempt({'requested': [1.] * 14})
    item.release.set()
