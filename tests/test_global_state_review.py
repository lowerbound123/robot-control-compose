from collections import OrderedDict
from types import SimpleNamespace
import threading
import time

import pytest

from robot_control.global_state_file import read_state


def test_stop_gate_precedes_background_scheduling_but_allows_matching_ack(monkeypatch):
    import rc_policy_sdk.host as module
    host = module.PolicyHost.__new__(module.PolicyHost)
    host.condition = threading.Condition(threading.RLock())
    host.owner = 'owner'
    host.runtime_id = 'runtime'
    host.revoked_owners = set()
    host.stopped = OrderedDict()
    host.closing = host.shutting_down = host.cleanup_failed = False
    peer = SimpleNamespace(owner='owner', send=lambda packet: replies.append(packet), close=lambda: None)
    replies, forwarded, scheduled = [], [], []
    host.sessions = {'session': dict(peer=peer, closed=False, deadlines={}, commands={}, timeouts={'inference': 2})}
    host._send_runtime = forwarded.append
    host.stop = lambda *args, **kwargs: None
    messages = iter([
        dict(type='stop', rpc_id=1, runtime_id='runtime', release_timeout=2),
        dict(type='dispatch', rpc_id=2, runtime_id='runtime', session_id='session', event={'type': 'infer'}),
        dict(type='dispatch', rpc_id=3, runtime_id='runtime', session_id='session', event={'type': 'global_state_ack', 'rpc_id': 7}),
    ])
    def receive(_):
        try:
            return next(messages)
        except StopIteration:
            raise EOFError
    class DelayedThread:
        def __init__(self, **kwargs):
            scheduled.append(kwargs)
        def start(self):
            pass  # Deterministically keep the stop worker unscheduled.
    monkeypatch.setattr(module, 'Peer', lambda _: peer)
    monkeypatch.setattr(module, 'receive_message', receive)
    monkeypatch.setattr(module.threading, 'Thread', DelayedThread)
    host.serve_peer(SimpleNamespace(close=lambda: None))
    assert len(scheduled) == 1
    assert any(packet.get('rpc_id') == 2 and packet.get('error') for packet in replies)
    assert [packet['event']['type'] for packet in forwarded] == ['global_state_ack']
    assert host.closing and 'owner' in host.revoked_owners


def test_thousands_of_valid_fields_load_promptly(tmp_path):
    path = tmp_path / 'global.env'
    data = {f'k{number}': number for number in range(6000)}
    path.write_text(''.join(f'{key}={value}\n' for key, value in data.items()))
    start = time.monotonic()
    assert read_state(path, 1048576) == data
    assert time.monotonic() - start < 2, 'valid small-field files must not make startup quadratic'


def test_state_loading_honors_startup_cancellation(tmp_path):
    path = tmp_path / 'global.env'
    path.write_text('key=1\n')
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(RuntimeError, match='cancel'):
        read_state(path, 1024, cancel_event=cancel)
