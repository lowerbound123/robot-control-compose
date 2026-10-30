"""Recorder instrumentation observes real cache/transport boundaries only."""
from io import BytesIO
from types import SimpleNamespace
import json
import time

import numpy as np
import pytest
from PIL import Image
from rc_policy_sdk.images import ImagePool

from robot_control.robots import ACOneRobot, MockRobot
from robot_control.process import PolicyProcess
from robot_control.chain_process import ChainProcess
from robot_control.config import PolicyRegistry
from host_helpers import LocalPolicyProcess
from test_chain_config import add_chain, add_policy
from test_robots import ArmState, connect_fake, fake_ros as fake_ros, robot_config, wait_until


def test_mock_recording_snapshot_is_cache_only_and_has_stable_camera_identity(monkeypatch):
    clock = SimpleNamespace(mono=100., wall=1000.)
    monkeypatch.setattr(time, 'monotonic', lambda: clock.mono)
    monkeypatch.setattr(time, 'time', lambda: clock.wall)
    robot = MockRobot()
    robot.connect()
    try:
        state = robot.read_state()
        before = {arm: len(history) for arm, history in robot._history.items()}
        first = robot.recording_snapshot()
        clock.mono += .01
        clock.wall += .01
        second = robot.recording_snapshot()
        assert first['state'] == state == second['state']
        assert first['state_error'] is None
        assert {arm: len(history) for arm, history in robot._history.items()} == before
        for name, frame in first['frames'].items():
            assert frame['sequence'] == second['frames'][name]['sequence'] == 1
            assert frame['timestamp'] == second['frames'][name]['timestamp'] == 1000.
            assert frame['received'] == 100.
            assert frame['valid'] and frame['error'] is None
            assert not frame['image'].flags.writeable
            assert robot.read_images()[name][1] == 1000.
        clock.mono = 100.04
        clock.wall = 1000.04
        assert robot.recording_snapshot()['frames']['head']['sequence'] == 2
        assert robot.read_images()['head'][1] == pytest.approx(1000. + 1 / 30)
        assert not robot.commands
    finally:
        robot.close()


def test_acone_recording_retains_invalid_frames_and_reads_without_control_readiness(fake_ros):
    robot = ACOneRobot(robot_config(camera_topics={'head': 'camera'}, camera_timeout_s=.02))
    connect_fake(robot, fake_ros)
    encoded = BytesIO()
    Image.fromarray(np.full((2, 3, 3), 77, np.uint8)).save(encoded, format='PNG')
    message = SimpleNamespace(data=encoded.getvalue(), header=SimpleNamespace(stamp=SimpleNamespace(sec=123, nanosec=4)))
    try:
        fake_ros.callbacks['camera'](message)
        wait_until(lambda: bool(robot._images))
        first = robot.recording_snapshot()
        assert first['state']['qpos'] == [1, 2, 3, 4, 5, 6, 7, 10, 11, 12, 13, 14, 15, 16]
        frame = first['frames']['head']
        assert frame['valid'] and frame['error'] is None
        assert frame['timestamp'] == pytest.approx(123.000000004)
        assert frame['sequence'] == 1 and not frame['image'].flags.writeable
        robot._connected = False
        during_connect = robot.recording_snapshot()
        assert during_connect['frames']['head']['image'] is frame['image']
        robot._connected = True
        with robot._condition:
            raw, timestamp, received = robot._feedback['left']
            robot._feedback['left'] = raw, timestamp, received - 10
            robot._images['head'] = frame['image'], frame['timestamp'], frame['received'] - 10
        stale = robot.recording_snapshot()
        assert 'stale left' in stale['state_error']
        assert stale['state']['qpos'] == first['state']['qpos']
        assert not stale['frames']['head']['valid']
        assert 'stale' in stale['frames']['head']['error']
        assert stale['frames']['head']['image'] is frame['image']
        with pytest.raises(RuntimeError, match='stale left'):
            robot.read_state()
        with pytest.raises(RuntimeError, match='stale camera'):
            robot.read_images()
        malformed = ArmState()
        malformed.joint_pos = []
        fake_ros.callbacks['left'](malformed)
        assert 'joint_pos' in robot.recording_snapshot()['state_error']
        assert robot.recording_snapshot()['state'] == stale['state']
        assert not any(fake_ros.published.values())
    finally:
        robot.close()


def test_acone_recording_marks_decode_failure_without_replacing_last_frame(fake_ros):
    robot = ACOneRobot(robot_config(camera_topics={'head': 'camera'}))
    connect_fake(robot, fake_ros)
    encoded = BytesIO()
    Image.fromarray(np.full((2, 3, 3), 55, np.uint8)).save(encoded, format='PNG')
    try:
        fake_ros.callbacks['camera'](SimpleNamespace(data=encoded.getvalue()))
        wait_until(lambda: bool(robot._images))
        first = robot.recording_snapshot()['frames']['head']
        fake_ros.callbacks['camera'](SimpleNamespace(data=b'bad image'))
        wait_until(lambda: bool(robot._image_errors))
        frame = robot.recording_snapshot()['frames']['head']
        assert not frame['valid'] and 'decode' in frame['error']
        assert frame['sequence'] == first['sequence'] and frame['image'] is first['image']
    finally:
        robot.close()


@pytest.mark.parametrize('failing_observer', [False, True])
def test_successful_arm_publish_is_recorded_before_other_arm_failure(fake_ros, failing_observer):
    robot = ACOneRobot(robot_config())
    connect_fake(robot, fake_ros)
    events = []
    def observe(event):
        events.append(event)
        assert len(fake_ros.published['command_left']) == 1
        if failing_observer:
            raise ValueError('recorder unavailable')
    robot.set_command_observer(observe)
    robot._publishers['right'].publish = lambda message: (_ for _ in ()).throw(OSError('transport gone'))
    try:
        with pytest.raises(RuntimeError, match='right.*already published arms=.*left'):
            robot.execute(np.arange(14))
        assert len(events) == 1
        assert events[0]['arm'] == 'left' and events[0]['mode'] == 'qpos'
        assert events[0]['values7'] == list(range(7))
        assert events[0]['timestamp_ns'] > 0 and events[0]['monotonic_ns'] > 0
        robot.set_command_observer(None)
        robot.execute_arms({'left': {'mode': 'epos', 'values': [0] * 7}})
        assert len(events) == 1
    finally:
        robot.close()


def test_mock_command_observer_sees_each_applied_arm_and_cannot_break_execution():
    robot = MockRobot()
    robot.connect()
    events = []
    def observe(event):
        events.append(event)
        raise RuntimeError('failed instrumentation')
    robot.set_command_observer(observe)
    try:
        robot.execute_arms({'left': {'mode': 'epos', 'values': [2] * 7},
                            'right': {'mode': 'qpos', 'values': [3] * 7}})
        assert [event['arm'] for event in events] == ['left', 'right']
        assert [event['mode'] for event in events] == ['epos', 'qpos']
        assert events[0]['values7'] == [2] * 7 and events[1]['values7'] == [3] * 7
        assert robot.read_state()['eef'][:7] == [2] * 7
        assert robot.read_state()['qpos'][7:] == [3] * 7
        assert len(robot.commands) == 1
    finally:
        robot.close()


def inference_request(request_id=41):
    return {'request_id': request_id, 'generation': 7, 'config_version': 2,
            'state': {'qpos': [0.] * 14}, 'action_chunk': [], 'images': {},
            'runtime': {'observation_id': 'obs-unique', 'consumed_actions': 3,
                        'chain_id': 'chain', 'chain_request_id': 9, 'cell': 2}}


def remote_worker(tmp_path, observer):
    worker = PolicyProcess(tmp_path / 'source', {'id': 'source-1', 'execution': {'control_mode': 'qpos'}},
                           2, host_socket='/tmp/unused-recording-host', on_trace=observer)
    worker._state = 'running'
    worker.runtime_id = 'runtime'
    worker.host = SimpleNamespace(enqueue=lambda packet: None)
    return worker


def receive_result(worker, request, **extras):
    message = {'type': 'result', **{key: request[key] for key in ('request_id', 'generation', 'config_version')},
               'actions': [[.5] * 14], 'images_released': True, **extras}
    worker._receive({'runtime_id': 'runtime', 'event': message})
    return message


def test_policy_trace_has_complete_correlated_payload_and_callback_errors_are_isolated(tmp_path):
    events = []
    def observer(event):
        events.append(event)
        raise ValueError('recorder queue failed')
    worker = remote_worker(tmp_path, observer)
    request = inference_request()
    worker.submit(request)
    result = receive_result(worker, request)
    assert worker.poll() == result
    assert [event['event'] for event in events] == ['policy.start', 'policy.result']
    for event in events:
        assert event['instance_id'] == 'source-1' and event['source_id'] == 'source'
        assert [event[key] for key in ('request_id', 'generation', 'config_version')] == [41, 7, 2]
        assert event['observation_id'] == 'obs-unique'
        assert [event[key] for key in ('chain_id', 'chain_request_id', 'cell')] == ['chain', 9, 2]
    assert events[0]['data']['state'] == request['state']
    assert events[0]['data']['images'] == {}
    assert events[1]['data'] == result
    assert events[1]['elapsed_s'] >= 0
    request['runtime']['cell'] = 99
    request['state']['later'] = True
    assert events[0]['data']['runtime']['cell'] == 2
    assert 'later' not in events[0]['data']['state']


def test_policy_trace_records_timeout_then_late_and_stale_results_without_reviving_control(tmp_path):
    events = []
    worker = remote_worker(tmp_path, events.append)
    request = inference_request()
    worker.submit(request)
    worker._inflight['deadline'] = 0
    assert worker.poll()['type'] == 'error'
    assert worker.poll() is None
    receive_result(worker, request)
    assert worker.poll() is None
    receive_result(worker, request)
    assert worker.poll() is None
    assert [event['event'] for event in events] == ['policy.start', 'policy.timeout', 'policy.late', 'policy.stale']
    assert all(event['observation_id'] == 'obs-unique' for event in events)
    assert events[2]['data']['actions'] == [[.5] * 14]


@pytest.mark.parametrize('terminal', ['error', 'cancel'])
def test_policy_trace_records_transport_failure_or_close(tmp_path, terminal):
    events = []
    worker = remote_worker(tmp_path, events.append)
    worker.submit(inference_request())
    if terminal == 'error':
        worker._record_failure('host disconnected')
        assert worker.poll()['error'] == 'host disconnected'
    else:
        worker.close()
    assert [event['event'] for event in events] == ['policy.start', 'policy.' + terminal]
    assert events[-1]['elapsed_s'] >= 0


def test_policy_trace_identifies_host_timeout_before_poll(tmp_path):
    events = []
    worker = remote_worker(tmp_path, events.append)
    worker.submit(inference_request())
    worker._record_failure('policy infer timeout')
    assert worker.poll()['error'] == 'policy infer timeout'
    assert events[-1]['event'] == 'policy.timeout'


def test_policy_trace_retains_original_correlation_for_old_runtime(tmp_path):
    events = []
    worker = remote_worker(tmp_path, events.append)
    request = inference_request()
    worker.submit(request)
    worker.runtime_id = 'replacement-runtime'
    receive_result(worker, request)
    assert worker.poll() is None
    assert events[-1]['event'] == 'policy.stale'
    assert events[-1]['observation_id'] == 'obs-unique'
    assert events[-1]['reason'] == 'runtime_id'
    assert events[-1]['data']['actions'] == [[.5] * 14]


def test_policy_trace_does_not_turn_unhashable_stale_identity_into_transport_failure(tmp_path):
    events = []
    worker = remote_worker(tmp_path, events.append)
    request = inference_request()
    worker.submit(request)
    receive_result(worker, {**request, 'request_id': []})
    assert worker.poll() is None
    result = receive_result(worker, request)
    assert worker.poll() == result


def build_chain(tmp_path, events, *, delay=0., timeout=2., fail=False):
    for identity in ('a', 'b'):
        config = add_policy(tmp_path, identity)
        config['params'].update(delay=delay, fail=fail and identity == 'b')
        (tmp_path / identity / 'policy.yaml').write_text(json.dumps(config))
        (tmp_path / identity / 'main.py').write_text('''
import time
def initialize(config): return config
def policy(context, state, action_chunk, images, config, *, runtime):
    time.sleep(config['params']['delay'])
    if config['params']['fail']: raise ValueError('intentional failure')
    source = action_chunk[0] if action_chunk else state['qpos']
    return [[value + 1 for value in source]]
def reset_context(context): pass
def release(context): pass
''')
    add_chain(tmp_path, ['a', 'b', 'a'])
    record = PolicyRegistry(tmp_path).get('default_chain')
    return ChainProcess(record, process_factory=LocalPolicyProcess, on_trace=events.append,
                        init_timeout_s=3, inference_timeout_s=timeout, release_timeout_s=.2)


def test_chain_trace_marks_expired_deadline_when_worker_fault_arrives_first(tmp_path):
    events = []
    worker = build_chain(tmp_path, events)
    worker._ready = True
    worker._workers['a-0'] = SimpleNamespace()
    request = inference_request()
    request['config_version'] = 1
    worker.submit(request)
    worker._request['deadline'] = 0
    worker._failure = 'chain a-0: worker exited'
    with worker._condition:
        worker._drive_inference()
    assert worker.poll()['type'] == 'error'
    assert events[-1]['event'] == 'policy.timeout'


def wait_result(worker):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = worker.poll()
        if result:
            return result
        time.sleep(.005)
    pytest.fail('inference did not finish')


@pytest.fixture
def recording_frames(tmp_path):
    pool = ImagePool(tmp_path / 'frames', cameras=('head',))
    pool.publish('head', np.zeros((2, 3, 3), np.uint8), 12.)
    lease = pool.acquire()
    yield lease.descriptors
    lease.release()
    pool.close()


def test_native_chain_trace_correlates_every_instance_and_keeps_original_inputs(tmp_path, recording_frames):
    events = []
    worker = build_chain(tmp_path, events)
    request = inference_request()
    request['config_version'] = 1
    request['images'] = recording_frames
    request['runtime'] = {'observation_id': 'chain-obs', 'consumed_actions': 3}
    try:
        worker.start()
        worker.submit(request)
        result = wait_result(worker)
        assert result['type'] == 'result', result
        assert result['actions'] == [[3.] * 14]
        starts = [event for event in events if event['event'] == 'policy.start']
        assert [event['instance_id'] for event in starts] == ['default_chain', 'a-0', 'b', 'a-1']
        assert [event['source_id'] for event in starts] == ['default_chain', 'a', 'b', 'a']
        assert starts[0]['data']['action_chunk'] == []
        assert [event['data']['action_chunk'] for event in starts[1:]] == [[], [[1.] * 14], [[2.] * 14]]
        for cell, event in enumerate(starts[1:], 1):
            assert event['chain_id'] == 'default_chain' and event['chain_request_id'] == 41
            assert event['cell'] == cell and event['observation_id'] == 'chain-obs'
            assert event['data']['runtime']['consumed_actions'] == 3
        assert 'cell' not in request['runtime']
        results = [event for event in events if event['event'] == 'policy.result']
        assert [event['instance_id'] for event in results] == ['a-0', 'b', 'a-1', 'default_chain']
        assert results[-1]['data'] == result
    finally:
        worker.close()


@pytest.mark.parametrize('terminal', ['cancel', 'timeout', 'error'])
def test_native_chain_trace_reports_aggregate_terminal_without_starting_later_cells(tmp_path, terminal, recording_frames):
    events = []
    worker = build_chain(tmp_path, events, delay=.15 if terminal != 'error' else 0,
                         timeout=.03 if terminal == 'timeout' else 2., fail=terminal == 'error')
    request = inference_request()
    request['config_version'] = 1
    request['images'] = recording_frames
    try:
        worker.start()
        worker.submit(request)
        if terminal == 'cancel':
            wait_until(lambda: any(event['instance_id'] == 'a-0' for event in events))
            worker.cancel_inference()
        result = wait_result(worker)
        assert result['type'] == ('cancelled' if terminal == 'cancel' else 'error'), result
        aggregate = [event for event in events if event['instance_id'] == 'default_chain']
        assert aggregate[-1]['event'] == 'policy.' + terminal
        assert aggregate[-1]['elapsed_s'] >= 0
        assert not any(event['instance_id'] == 'a-1' for event in events)
    finally:
        worker.close()
