"""Fixtures at the remote service boundary; no model or hardware dependencies."""
import copy
from pathlib import Path
import sys
import queue
import threading
import time
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'packages/internvla-maas-policy/src'
sys.path.insert(0, str(PACKAGE))


def settings(address='ws://127.0.0.1:9000'):
    return {
        'service': {'address': address, 'model': 'internvla_a15', 'robot_id': 'test-robot',
                    'api_key': 'fixture-key', 'embodiment': 'test-dual-arm'},
        'runtime': {'instruction': 'Move the object into the tray.'},
    }


def configured(tmp_path, value=None):
    from rc_internvla_maas.configuration import load_model_config
    path = tmp_path / 'model.yaml'
    write_model_config(path, settings() if value is None else copy.deepcopy(value))
    return load_model_config(path, project_root=tmp_path)


def write_model_config(path, value):
    from ruamel.yaml import YAML
    with path.open('w', encoding='utf-8') as stream:
        YAML(typ='safe', pure=True).dump(value, stream)


@contextmanager
def model_server(*, host='127.0.0.1', hello_patch=None, reject_status=None, close_code=None,
                 hello_delay=0, ready_delay=0):
    """Real WebSocket peer using the successful diagnostic script's independent codec."""
    from internvla_maas_reference import packb, unpackb
    import numpy as np
    from websockets.sync.server import serve
    from websockets.exceptions import ConnectionClosed
    from websockets.protocol import State

    class Service:
        requests = None
        hellos = None
        connections = None
        def __init__(self):
            self.requests, self.hellos, self.connections = [], [], set()
            self.responses = queue.Queue()
            self.lock = threading.Lock()
            self.stopping = threading.Event()
            self.ids = {}
            self.closed = 0

        def upgrade(self, connection, request):
            if reject_status or request.headers.get('Authorization') != 'Api-Key fixture-key':
                return connection.respond(reject_status or 401, 'denied')

        def handler(self, connection):
            robot_id = None
            registered = False
            with self.lock:
                self.connections.add(connection)
            try:
                hello = {'type': 'server_hello', 'model': 'internvla_a15', 'protocol': 'maas-robot/1',
                         'requires': {'images': {n: [224, 224] for n in ('front', 'wrist_left', 'wrist_right')},
                                      'state': ['joint_pos']},
                         'provides': {'space': 'joint_pos', 'fields': ['joint_pos'], 'horizon': 50, 'control_hz': 30.0}}
                hello.update(hello_patch or {})
                if hello_delay:
                    time.sleep(hello_delay)
                connection.send(packb(hello))
                robot = unpackb(connection.recv())
                robot_id = robot['robot_id']
                with self.lock:
                    self.hellos.append({'message': robot, 'path': connection.request.path})
                    owner = self.ids.get(robot_id)
                    # A delayed mock inference handler may outlive its socket.
                    # Only an OPEN connection reserves the online robot identity.
                    if owner is None or owner.state != State.OPEN:
                        self.ids[robot_id] = connection
                        registered = True
                if close_code or not registered:
                    connection.close(close_code or 4409, 'fixture-key must be redacted')
                    return
                if ready_delay:
                    time.sleep(ready_delay)
                connection.send(packb({'type': 'ready', 'session_id': str(len(self.hellos)),
                                                    'revision': 'test', 'checkpoint': 'fixture'}))
                for frame in connection:
                    obs = unpackb(frame)
                    with self.lock:
                        self.requests.append(obs)
                    try:
                        response = self.responses.get_nowait()
                    except queue.Empty:
                        response = {}
                    gate = response.get('gate')
                    while gate is not None and not gate.wait(.01):
                        if self.stopping.is_set() or connection.state != State.OPEN:
                            return
                    if 'close' in response:
                        connection.close(response['close'], 'fixture-key connection failed')
                        return
                    if 'raw' in response:
                        connection.send(response['raw'])
                        continue
                    actions = np.repeat(obs['state']['joint_pos'][None], 50, axis=0)
                    body = response.get('body', {'type': 'actions', 'space': 'joint_pos', 'mode': 'absolute',
                                                'server_ms': np.float32(1.25), 'actions': {'joint_pos': actions}})
                    connection.send(packb(body))
            except ConnectionClosed:
                pass
            finally:
                with self.lock:
                    self.connections.discard(connection)
                    if registered and self.ids.get(robot_id) is connection:
                        del self.ids[robot_id]
                    self.closed += 1

    service = Service()
    with serve(service.handler, host, 0, process_request=service.upgrade,
               max_size=32 * 1024 * 1024, close_timeout=.5, compression=None) as server:
        service.port = server.socket.getsockname()[1]
        service.address = f'ws://{host}:{service.port}'
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield service
        finally:
            service.stopping.set()
            for connection in list(service.connections):
                connection.close()
            server.shutdown()
            thread.join(3)


def wait(predicate, timeout=5):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError('MaaS test condition timed out')


def images():
    import numpy as np
    return {name: np.full((2, 3, 3), index, np.uint8)
            for index, name in enumerate(('head', 'left_wrist', 'right_wrist'), 1)}
