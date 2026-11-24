"""Exercise the split connector through the normal SDK entry point and sockets."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

import pytest

from rc_connector_sdk.client import ConnectorClient
from test_connector_transport import results, target


DEVICE = """
import os
from pathlib import Path
import time

class Device:
    camera_topics = ('head',)

    def __init__(self, config, role, watermarks):
        self.role, self.watermarks = role, watermarks
        self.fail_start = config.get('fail_start')
        self.hold_query = config.get('hold_query')
        self.stall_close = config.get('stall_close')

    def connect(self, cancel_event):
        Path(__file__).with_suffix('.' + self.role).write_text(str(os.getpid()))
        if self.role == 'query':
            if self.hold_query:
                while not Path(__file__).with_suffix('.activate').exists():
                    if cancel_event.wait(.01): raise RuntimeError('cancelled')
            self.watermarks.update({'left': 41, 'right': 19}, time.monotonic() + 60)
        elif self.fail_start:
            raise RuntimeError('command setup failed')

    def set_command_observer(self, observer):
        assert self.role == 'command'
        self.observer = observer

    def execute_arms(self, targets):
        assert self.role == 'command'
        for arm, target in targets.items():
            if target['values'][0] == 99:
                raise RuntimeError('right publisher failed')
            self.observer({'arm': arm, 'pid': os.getpid()})

    def read_state(self):
        assert self.role == 'query'
        return {'query_pid': os.getpid()}

    def recording_snapshot(self):
        return {'frames': {}}

    def close(self, *, deadline):
        assert isinstance(deadline, float)
        if self.role == 'command':
            if self.stall_close: time.sleep(10)
            Path(__file__).with_suffix('.closed').touch()

def create_split_connector(config, *, role, watermarks):
    return Device(config, role, watermarks)
"""


@pytest.mark.parametrize('ending', ['drain', 'child_lost', 'startup_failed', 'parent_lost', 'forced_close'])
def test_split_command_lifecycle(tmp_path, ending):
    source = tmp_path / 'device.py'
    source.write_text(DEVICE)
    sdk = Path(__file__).resolve().parents[1] / 'packages/connector-sdk/src'
    with tempfile.TemporaryDirectory(dir='/tmp') as directory:
        endpoint = directory + '/io'
        config = {'fail_start': ending == 'startup_failed', 'hold_query': ending == 'drain',
                  'stall_close': ending == 'forced_close'}
        process = subprocess.Popen([sys.executable, '-m', 'rc_connector_sdk', '--source', str(source),
            '--socket', endpoint, '--config-json', json.dumps(config)],
            env={**os.environ, 'PYTHONPATH': str(sdk)})
        client = None
        try:
            ready = Path(endpoint + '.ready')
            deadline = time.monotonic() + 8
            if ending == 'drain':
                while not source.with_suffix('.command').exists():
                    assert process.poll() is None and time.monotonic() < deadline
                    time.sleep(.01)
                assert not ready.exists()
                with socket.socket(socket.AF_UNIX) as early, pytest.raises(ConnectionRefusedError):
                    early.connect(endpoint + '.command')
                source.with_suffix('.activate').touch()
            while not ready.exists() and process.poll() is None:
                assert time.monotonic() < deadline
                time.sleep(.01)
            if ending == 'startup_failed':
                assert not ready.exists() and process.wait(timeout=3) != 0
            else:
                client = ConnectorClient(endpoint)
                query_pid = client.get_state()['state']['query_pid']
                command_pid = int(source.with_suffix('.command').read_text())
                assert query_pid == process.pid and command_pid != query_pid
                ids = [client.submit(target(i)) for i in range(4)]
                partial = client.submit({**target(5), 'right': target(99)['left']})
                rows = results(client, 5)
                assert [r['id'] for r in rows] == ids + [partial]
                assert all(r['success'] for r in rows[:-1])
                assert not rows[-1]['success'] and rows[-1]['arms'] == {'left': 'published', 'right': 'unknown'}
                for r in rows:
                    assert r['events'] == [{'arm': 'left', 'pid': command_pid}]
                    assert r['feedback_watermarks'] == {'left': 41, 'right': 19}
                if ending == 'drain':
                    last = client.submit(target(6))
                    drained = client.close(timeout=3)
                    assert [(r['id'], r['success']) for r in drained] == [(last, True)]
                    assert process.wait(timeout=3) == 0
                elif ending == 'parent_lost':
                    process.kill()
                    assert process.wait(timeout=3) != 0
                    deadline = time.monotonic() + 3
                    while not source.with_suffix('.closed').exists() or not client.failure:
                        assert time.monotonic() < deadline
                        time.sleep(.01)
                elif ending == 'forced_close':
                    started = time.monotonic()
                    client.close(timeout=.5)
                    assert process.wait(timeout=2) != 0
                    assert time.monotonic() - started < 2
                    assert not source.with_suffix('.closed').exists()
                else:
                    os.kill(command_pid, signal.SIGKILL)
                    assert process.wait(timeout=3) != 0
                    with pytest.raises(RuntimeError, match='disconnected|closed'):
                        client.submit(target(7))
            if ending != 'parent_lost':  # SIGKILL leaves the instance directory to the runtime owner.
                assert all(not Path(endpoint + suffix).exists() for suffix in ('.query', '.command', '.ready'))
        finally:
            if client is not None: client.disconnect()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=7)
