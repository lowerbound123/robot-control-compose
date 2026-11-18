"""ROS-boundary timestamps travel through the connector sockets into the RC debug log."""
from pathlib import Path
import tempfile
import threading
import time

import pytest

from connector_helpers import ACOneRobot
from rc_connector_sdk.client import ConnectorClient
from rc_connector_sdk.performance import FeedbackTimings
from rc_connector_sdk.server import ConnectorServer
from robot_control.connector import ConnectorRobot
from robot_control.performance import PerformanceLogger
from test_connector_lift2 import device, eef, joint, lift2
from test_robots import ArmState, connect_fake, fake_ros, robot_config, wait_until


@pytest.fixture(params=['acone', 'lift2'])
def hardware(request):
    if request.param == 'acone':
        node = request.getfixturevalue('fake_ros')
        driver = ACOneRobot(robot_config())
        connect_fake(driver, node)
        yield 'acone', driver, lambda: node.callbacks['right'](ArmState()), 'status'
        driver.close()
    else:
        driver, _, _ = request.getfixturevalue('device')
        yield 'lift2', driver, lambda: driver._on_eef('right', eef()), 'eef'


@pytest.mark.parametrize('level', ['INFO', 'DEBUG', 'TRACE'])
def test_each_publication_and_feedback_has_its_own_driver_timestamp(hardware, level, monkeypatch):
    source, driver, feedback, kind = hardware
    events = []
    log = PerformanceLogger(level, 'run', sink=events.append)
    proxy = ConnectorRobot({'robot': {'connector': 'body'}, 'connector_directory': 'connector',
                            'connectors': {'body': {'source': source, 'config': 'unused'}}}, '.')
    proxy.performance = log if log.enabled else None
    with tempfile.TemporaryDirectory(dir='/tmp', prefix='timing-') as folder:
        endpoint = folder + '/io'
        server = ConnectorServer(driver, endpoint, debug_timing=proxy._debug_ros_timing)
        thread = threading.Thread(target=server.serve, daemon=True)
        thread.start()
        try:
            wait_until(lambda: Path(endpoint + '.ready').exists())
            proxy.client = ConnectorClient(endpoint, on_feedback_timing=proxy._log_feedback_timing if log.enabled else None)
            start_ns = time.time_ns()
            feedback()
            feedback()
            if source == 'lift2':
                driver._on_joint('left', joint())
            end_ns = time.time_ns()
            for _ in range(2):
                proxy.client.get_state()
            targets = {arm: {'mode': 'qpos', 'values': [0.] * 7} for arm in ('left', 'right')}
            identity = proxy.execute_arms(targets)
            proxy.bind_attempt(identity, 'attempt')
            wait_until(proxy.poll_results)
            assert proxy.publication(identity)['success']
            def fail_publish(message):
                raise RuntimeError('test publication failure')
            monkeypatch.setattr(driver._publishers['right'], 'publish', fail_publish)
            partial = proxy.execute_arms(targets)
            proxy.bind_attempt(partial, 'partial')
            wait_until(proxy.poll_results)
            assert not proxy.publication(partial)['success']
            feedback()  # Normal close flushes this callback without another state poll.
        finally:
            proxy.close()
            server.stop.set()
            thread.join(2)
            assert log.close(time.monotonic() + 2)
    if level == 'INFO':
        assert not events
        return
    sends = [e for e in events if e['stage'] == 'connector.command_sent']
    assert [(e['arm'], e['connector_request_id'], e['attempt_id']) for e in sends] == [
        ('left', identity, 'attempt'), ('right', identity, 'attempt'), ('left', partial, 'partial')]
    for entry, publication in zip(sends, proxy.publication(identity)['events'] + proxy.publication(partial)['events']):
        assert entry['command_sent_timestamp_ns'] == publication['timestamp_ns']
        assert entry['command_sent_monotonic_ns'] == publication['monotonic_ns']
        assert 'values7' not in entry
    received = [e for e in events if e['stage'] == 'connector.feedback_received']
    expected = [('right', kind)] * 2 + ([('left', 'joint')] if source == 'lift2' else []) + [('right', kind)]
    assert [(e['arm'], e['feedback_kind']) for e in received] == expected
    assert all(start_ns <= e['feedback_received_timestamp_ns'] <= end_ns for e in received[:-1])
    assert all(e['feedback_received_monotonic_ns'] > 0 and 'connector_request_id' not in e for e in received)
    assert all(e['instance_id'] == 'body' and e['source_id'] == source for e in sends + received)


def test_feedback_overflow_is_counted_and_close_ends_collection():
    timing = FeedbackTimings(capacity=2)
    for _ in range(5):
        timing.received('left', 'joint')
    reply = {}
    timing.drain_into(reply, closing=True)
    assert len(reply['_feedback_timing']['events']) == 2
    assert reply['_feedback_timing']['dropped'] == 3
    timing.received('right', 'eef')
    empty = {}
    timing.drain_into(empty)
    assert empty == {}


def test_failed_timing_observer_leaves_feedback_available(hardware):
    _, driver, deliver, _ = hardware
    def broken(arm, kind):
        raise RuntimeError('test diagnostic failure')
    driver.set_feedback_observer(broken)
    if isinstance(driver, ACOneRobot):
        driver._on_feedback('right', ArmState(20))
    else:
        driver._on_joint('right', joint(20))
        deliver()
    assert driver.read_state()['qpos'][7] == 20
