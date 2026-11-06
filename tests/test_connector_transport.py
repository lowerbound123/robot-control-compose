"""Real independent connector process: FIFO device I/O and leased image snapshots."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'packages/connector-sdk/src'))


@pytest.fixture
def connector(tmp_path):
    from rc_connector_sdk.client import ConnectorClient
    source = tmp_path / 'device.py'
    source.write_text('''
import time
import numpy as np
class Device:
    camera_topics = {'head': 'mock'}
    def __init__(self, config):
        self.values = [0.] * 14
        self.observer = None
        self.image = np.arange(18, dtype=np.uint8).reshape(2, 3, 3)
    def connect(self, cancel_event=None): pass
    def read_state(self): return {'qpos': self.values, 'sequences': {'left': 1, 'right': 1}}
    def feedback_since(self, arm, seq): return []
    def recording_snapshot(self):
        return {'state': self.read_state(), 'state_error': None, 'frames': {'head': {
            'image': self.image, 'timestamp': 123., 'received': time.monotonic(),
            'sequence': 7, 'valid': True, 'error': None}}}
    def set_command_observer(self, observer): self.observer = observer
    def execute_arms(self, targets):
        time.sleep(.015)
        for arm, target in targets.items():
            if target['values'][0] == 99: raise RuntimeError('injected publish failure')
            i = 0 if arm == 'left' else 7
            self.values[i:i+7] = target['values']
            self.image = np.full((2, 3, 3), int(target['values'][0]), dtype=np.uint8)
            if self.observer: self.observer({'arm': arm, 'mode': target['mode'], 'values7': target['values']})
    def close(self): pass
def create_connector(config): return Device(config)
''')
    socket_dir = tempfile.TemporaryDirectory(prefix='rcc-', dir='/tmp')
    endpoint = socket_dir.name + '/io'
    env = dict(os.environ, PYTHONPATH=str(ROOT / 'packages/connector-sdk/src'))
    process = subprocess.Popen([sys.executable, '-m', 'rc_connector_sdk', '--source', str(source),
        '--socket', endpoint, '--config-json', '{}'], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    client = None
    try:
        deadline = time.monotonic() + 5
        while not Path(endpoint + '.ready').exists():
            if process.poll() is not None:
                raise AssertionError(process.communicate()[1].decode())
            assert time.monotonic() < deadline
            time.sleep(.01)
        client = ConnectorClient(endpoint)
        yield client
        client.close(timeout=3)
        assert process.wait(timeout=3) == 0, process.communicate()[1].decode()
    finally:
        if client is not None: client.disconnect()
        if process.poll() is None:
            process.terminate()
            try: process.wait(timeout=3)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        socket_dir.cleanup()


def target(value):
    return {'left': {'mode': 'qpos', 'values': [float(value)] * 7}}


def results(client, count):
    received = []
    deadline = time.monotonic() + 3
    while len(received) < count:
        received.extend(client.poll_results())
        assert time.monotonic() < deadline
        time.sleep(.002)
    return received


def test_fifo_receipts_and_queries_are_independent(connector):
    identities = [connector.submit(target(i)) for i in range(8)]
    assert connector.get_state()['state']['qpos'] is not None
    receipts = results(connector, 8)
    assert [r['id'] for r in receipts] == identities
    assert all(r['success'] for r in receipts)
    assert connector.get_state()['state']['qpos'][:7] == [7.] * 7


def test_partial_publish_is_reported_without_replay(connector):
    connector.submit({**target(3), 'right': {'mode': 'qpos', 'values': [99.] * 7}})
    receipt = results(connector, 1)[0]
    assert not receipt['success']
    assert receipt['arms'] == {'left': 'published', 'right': 'unknown'}
    assert [e['arm'] for e in receipt['events']] == ['left']
    assert connector.poll_results() == []


def test_images_are_requested_shared_memory_and_explicitly_released(connector):
    from rc_connector_sdk.images import map_images
    before = connector.describe()
    assert before['image_bytes'] == 0
    response = connector.get_images(['head'])
    descriptors = response['images']
    assert 'image' not in descriptors['head']
    assert len(json.dumps(response)) < 2000
    with map_images(descriptors) as frames:
        assert not frames['head'].flags.writeable
        assert np.array_equal(frames['head'], np.arange(18, dtype=np.uint8).reshape(2, 3, 3))
        copied = frames['head'].copy()
        assert connector.describe()['leases'] == 1
    connector.release_images([response['lease_id']])
    connector.release_images([response['lease_id']])
    assert connector.describe()['leases'] == 0
    assert np.array_equal(copied, np.arange(18, dtype=np.uint8).reshape(2, 3, 3))


def test_image_pool_never_overwrites_borrowed_slots(connector):
    offered = [connector.get_images(['head']) for _ in range(3)]
    with pytest.raises(RuntimeError, match='capacity|slot'):
        connector.get_images(['head'])
    assert connector.describe()['leases'] == 3
    connector.release_images([r['lease_id'] for r in offered])


def test_shutdown_drains_already_submitted_commands(connector):
    for i in range(8): connector.submit(target(i))
    connector.close(timeout=3)
    assert connector.closed


def test_slow_reader_keeps_its_pixels_while_new_frame_is_published(connector):
    from rc_connector_sdk.images import map_images
    old = connector.get_images(['head'])
    with map_images(old['images']) as old_frames:
        expected = old_frames['head'].copy()
        connector.submit(target(42))
        results(connector, 1)
        new = connector.get_images(['head'])
        with map_images(new['images']) as new_frames:
            assert (new_frames['head'] == 42).all()
            np.testing.assert_array_equal(old_frames['head'], expected)
        connector.release_images([new['lease_id']])
    connector.release_images([old['lease_id']])


def test_command_disconnect_does_not_release_image_reader(connector):
    import socket
    old = connector.get_images(['head'])
    connector._command.shutdown(socket.SHUT_RDWR)
    time.sleep(.02)
    assert connector.describe()['leases'] == 1
    connector.release_images([old['lease_id']])


def test_unpolled_receipts_cannot_make_command_memory_unbounded(connector):
    for i in range(32): connector.submit(target(i))
    with pytest.raises(RuntimeError, match='queue is full'): connector.submit(target(50))
    assert len(results(connector, 32)) == 32
    assert connector.get_state()['state']['qpos'][:7] == [31.] * 7


def test_validation_error_receipt_preserves_command_fifo(connector):
    from rc_connector_sdk.wire import send_message
    first = connector.submit(target(3))
    # Simulate a malformed peer frame after one valid, slow device call.
    with connector._lock:
        connector.serial += 1
        second = connector.serial
        connector._pending[second] = target(4)
        send_message(connector._command, {'id': second, 'method': 'send_targets',
                                         'params': {'left': {'mode': 'bad', 'values': [4] * 7}}})
    rows = results(connector, 2)
    assert [row['id'] for row in rows] == [first, second]
    assert rows[0]['success'] and not rows[1]['success']
    assert rows[1]['arms'] == {'left': 'not_published'}


def test_close_does_not_add_a_second_wait_for_a_busy_receipt_callback(connector):
    import threading
    entered, finish = threading.Event(), threading.Event()
    def wake():
        entered.set()
        finish.wait(2)
    connector.wake = wake
    try:
        connector.submit(target(1))
        assert entered.wait(1)
        started = time.monotonic()
        connector.close(timeout=.05)
        assert time.monotonic() - started < .2
    finally:
        finish.set()
        connector._reader.join(1)
