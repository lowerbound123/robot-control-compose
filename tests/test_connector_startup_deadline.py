"""Real UDS responses may not extend or escape the startup transaction budget."""
from contextlib import contextmanager
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

from rc_connector_sdk.wire import receive_message, send_message
from robot_control.connector import ConnectorRobot


@contextmanager
def delayed_connector(tmp_path, delayed_method, *, delay=.3, cancel=None):
    temporary = tempfile.TemporaryDirectory(prefix='rc-deadline-', dir='/tmp')
    endpoint = temporary.name + '/io'
    listeners = []
    for suffix in ('.query', '.command'):
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(endpoint + suffix)
        listener.listen(2)
        listener.settimeout(.05)
        listeners.append(listener)
    Path(endpoint + '.ready').write_text('ready')
    stopped = threading.Event()
    peers = []
    calls = []
    def serve():
        while not stopped.is_set():
            try:
                peer, _ = listeners[0].accept()
                break
            except socket.timeout: continue
            except OSError: return
        else: return
        peers.append(peer)
        try:
            while not stopped.is_set():
                request = receive_message(peer)
                method = request['method']
                calls.append(method)
                if method == delayed_method:
                    if cancel is not None: cancel.set()
                    time.sleep(delay)
                result = {
                    'describe': {'protocol_version': 1, 'instance_id': 'body', 'runtime_id': 'runtime', 'pool_id': '0123456789ab'},
                    'get_state': {'state': {'qpos': [0.] * 14}, 'state_error': None, 'history': {}},
                    'get_images': {'images': {}, 'metadata': {}, 'lease_id': 'lease-1'},
                    'release_images': {'released': True}, 'close': {'clean': True},
                }[method]
                send_message(peer, {'result': result})
        except (OSError, EOFError): pass
    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    robot = ConnectorRobot({'connector_directory': '.',
        'connectors': {'body': {'source': 'mock', 'config': 'device.yaml'}},
        'robot': {'connector': 'body'}}, tmp_path)
    robot._prepared, robot.config = True, {}
    robot.runtime = SimpleNamespace(
        start_connector=lambda *args, **kwargs: SimpleNamespace(socket=endpoint),
        connector_ready=lambda *args: None,
        stop_and_remove=lambda *args: SimpleNamespace(forced=False),
        forget_pool=lambda *args: None)
    try:
        yield robot, calls
    finally:
        stopped.set()
        for peer in peers:
            try: peer.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            peer.close()
        for listener in listeners: listener.close()
        worker.join(1)
        if robot.client is not None: robot.client.disconnect(timeout=.1)
        temporary.cleanup()


@pytest.mark.parametrize('method', ['describe', 'get_state', 'get_images', 'release_images'])
def test_startup_queries_share_absolute_deadline(tmp_path, method):
    with delayed_connector(tmp_path, method) as (robot, calls):
        started = time.monotonic()
        with pytest.raises((TimeoutError, RuntimeError, OSError)):
            robot.connect(deadline=started + .05)
        assert time.monotonic() - started < .15
        assert robot._sampler is None
        assert robot._state is None
        if method == 'release_images':
            assert not robot.closed and not robot._client_close_confirmed


def test_startup_cancellation_interrupts_delayed_state_and_cannot_commit(tmp_path):
    cancel = threading.Event()
    with delayed_connector(tmp_path, 'get_state', cancel=cancel) as (robot, calls):
        started = time.monotonic()
        with pytest.raises((TimeoutError, RuntimeError, OSError)):
            robot.connect(cancel_event=cancel, deadline=started + 2)
        assert time.monotonic() - started < .2
        assert robot._sampler is None and robot._state is None


def test_background_sampler_does_not_reuse_expired_startup_budget(tmp_path):
    with delayed_connector(tmp_path, 'unused') as (robot, calls):
        started = time.monotonic()
        robot.connect(deadline=started + .1)
        try:
            time.sleep(.15)
            assert robot._sampler.is_alive() and robot._state_error is None
            assert robot._state_received > started + .1
            assert calls.count('get_state') > 2
        finally:
            robot.close(deadline=time.monotonic() + 1)


@pytest.mark.parametrize('deadline', [0, -1])
def test_expired_startup_never_launches_connector(tmp_path, deadline):
    with delayed_connector(tmp_path, 'unused') as (robot, calls):
        def forbidden(*args, **kwargs):
            pytest.fail('expired startup launched connector')
        robot.runtime.start_connector = forbidden
        with pytest.raises(TimeoutError):
            robot.connect(deadline=deadline)
        assert not calls and robot._sampler is None
