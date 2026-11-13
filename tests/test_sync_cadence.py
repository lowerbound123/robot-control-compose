"""Publication ACKs must not cost synchronous policies an extra send tick."""
import time
import json

import numpy as np
import pytest

from test_connector_control import delayed_robot, receipt
from test_controller import ready, setup_controller, until
from test_example_controller import ROOT, command, example_rc
from config_helpers import read_config
from project_templates import copy_templates


def test_sync_callback_starts_on_ack_before_next_send_tick(setup_controller, monkeypatch):
    controller, robot, _ = setup_controller
    controller.config['frequency_hz'] = 2
    submitted, receipts = delayed_robot(monkeypatch, robot)
    controller._bind_connector_runtime(robot)
    worker = ready(controller)
    with controller._lock:
        controller.registry.get('first').config['execution'].update(mode='sync', call_every_actions=1)
        controller._state = 'running'
        controller._needs_request = False
        controller.buffer.install(np.array([[.01] * 14, [.02] * 14]))
        controller._tick()
        assert controller.buffer.consumed == 0 and not worker.requests
        identity = submitted[-1][0]
        before = len(submitted)
    receipts.append(receipt(identity))
    robot.wake()
    # The next 500ms send tick is still far away. A cached callback should
    # start on the receipt wakeup, leaving that tick available for its result.
    until(lambda: len(worker.requests) == 1, timeout=.2)
    with controller._lock:
        assert controller.buffer.consumed == 1
        assert worker.requests[0]['runtime']['consumed_actions'] == 1
        np.testing.assert_allclose(worker.requests[0]['action_chunk'], [[.02] * 14])
        assert len(submitted) == before
    # Starting early does not turn sync into async: even a full send period
    # may pass without sending another target while this callback is pending.
    time.sleep(.55)
    with controller._lock:
        assert len(submitted) == before
        worker.finish([[.02] * 14])
    until(lambda: len(submitted) > before, timeout=1)
    np.testing.assert_allclose(submitted[before][1], [.02] * 14)


@pytest.mark.parametrize('call_every_actions', [1, 2])
def test_fast_sync_callbacks_keep_30hz_with_async_publication(
        setup_controller, monkeypatch, call_every_actions):
    controller, robot, _ = setup_controller
    controller.config['frequency_hz'] = 30
    submitted, receipts = delayed_robot(monkeypatch, robot)
    controller._bind_connector_runtime(robot)
    publish = robot.execute
    target_times = []

    def acknowledge(values, mode='qpos'):
        identity = publish(values, mode)
        if values[6] >= 0:
            target_times.append(time.monotonic())
        receipts.append(receipt(identity))
        robot.wake()
        return identity

    monkeypatch.setattr(robot, 'execute', acknowledge)
    worker = ready(controller)
    submit = worker.submit

    def cached_result(request):
        submit(request)
        worker.finish(request['action_chunk'] or [[.01] * 14] * 100)

    monkeypatch.setattr(worker, 'submit', cached_result)
    with controller._lock:
        controller.registry.get('first').config['execution'].update(
            mode='sync', call_every_actions=call_every_actions)
    controller.submit('resume')
    until(lambda: len(target_times) >= 31, timeout=3)
    intervals = np.diff(target_times[:31])
    # Permit normal host jitter; the old alternating 33ms/67ms or 67ms
    # schedule fails these bounds. ACK wakeups must never burst-send.
    assert target_times[30] - target_times[0] < 1.3
    assert np.percentile(intervals, 90) < .05
    assert intervals.min() >= .8 / 30 - .002
    assert all(row[2] == 'rc-control' for row in submitted)


@pytest.mark.parametrize('interrupt', ['pause', 'take'])
def test_queued_control_change_prevents_ack_callback(setup_controller, monkeypatch, interrupt):
    controller, robot, _ = setup_controller
    controller.config['frequency_hz'] = 2
    submitted, receipts = delayed_robot(monkeypatch, robot)
    controller._bind_connector_runtime(robot)
    worker = ready(controller)
    with controller._lock:
        controller.registry.get('first').config['execution'].update(mode='sync', call_every_actions=1)
        controller._state = 'running'
        controller._needs_request = False
        controller.buffer.install(np.array([[.01] * 14, [.02] * 14]))
        controller._tick()
        receipts.append(receipt(submitted[-1][0]))
        operation = (controller.submit_control('take') if interrupt == 'take'
                     else controller.submit('pause'))
        robot.wake()
    assert controller.wait_operation(operation, 2)['status'] == 'succeeded'
    until(lambda: not receipts)
    with controller._lock:
        assert not worker.requests
        assert controller._state != 'running'


@pytest.mark.parametrize('chained', [False, True])
def test_example_and_self_chain_keep_30hz_through_real_connector(example_rc, chained):
    rc = example_rc
    manifest_path = rc.registry.directory / 'example/policy.yaml'
    manifest = read_config(manifest_path)
    manifest['execution'].update(mode='sync', call_every_actions=1)
    # Two real serial workers already add IPC/coordinator latency. Keep the
    # chain callback below the send period; exercise added latency separately
    # in the single-worker case and blocking semantics above.
    manifest['params']['inference_delay_s'] = 0 if chained else .008
    manifest_path.write_text(json.dumps(manifest))
    target = 'example'
    if chained:
        copy_templates(ROOT / 'policy/example_chain', rc.registry.directory / 'example_chain')
        target = 'example_chain'
    command(rc, f'policy switch {target}')
    published = []
    observer = rc._recorder.sent
    traces = []
    trace_sink = rc._worker.on_trace

    def trace(event):
        trace_sink(event)
        if event['event'] in ('policy.start', 'policy.result'):
            traces.append(event)

    rc._worker.on_trace = trace

    def observe(event):
        observer(event)
        if rc._state == 'running' and event['arm'] == 'left':
            published.append(event['monotonic_ns'] / 1e9)

    rc.robot.set_command_observer(observe)
    command(rc, 'resume')
    until(lambda: len(published) >= 36, timeout=5)
    command(rc, 'pause')
    intervals = np.diff(published[5:36])
    elapsed = published[35] - published[5]
    callback_ms = [event['elapsed_s'] * 1000 for event in traces if event['event'] == 'policy.result']
    (rc.root / 'cadence.json').write_text(json.dumps(dict(published=published,
        traces=[{key: value for key, value in event.items() if key != 'data'} for event in traces])))
    print(f'{target}: {30 / elapsed:.3f} Hz, median {np.median(intervals) * 1000:.3f} ms, '
          f'p90 {np.percentile(intervals, 90) * 1000:.3f} ms; '
          f'callback median/max {np.median(callback_ms):.3f}/{max(callback_ms):.3f} ms')
    assert elapsed < 1.3
    assert np.percentile(intervals, 90) < .05
