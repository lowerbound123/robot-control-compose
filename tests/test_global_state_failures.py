from contextlib import contextmanager
from concurrent.futures import Future
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from rc_policy_sdk.global_state import SharedState
from robot_control.global_state import GlobalStateService
from robot_control.global_state_file import save_state, read_state


@pytest.fixture
def service(tmp_path):
    obj = GlobalStateService(tmp_path / 'global.env', tmp_path / 'ipc', run_id='run', timeout_s=.2)
    obj.start()
    yield obj
    obj.close(time.monotonic() + 3)


def test_cross_process_reader_and_dead_reader_releases_lock(service):
    import json
    service.proxy['shared'] = {'hello': '中文'}
    code = '''
import json,sys
from rc_policy_sdk.global_state import SharedState
s=SharedState.attach(json.loads(sys.argv[1]))
print(json.dumps(s.snapshot()[1],ensure_ascii=False),flush=True)
with s.locked():
    print('locked',flush=True)
    sys.stdin.read()
'''
    child = subprocess.Popen([sys.executable, '-c', code, json.dumps(service.descriptor)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert json.loads(child.stdout.readline()) == {'shared': {'hello': '中文'}}
        assert child.stdout.readline().strip() == 'locked'
        with pytest.raises(TimeoutError):
            service.proxy['blocked'] = True
        child.kill()
        child.wait(2)
        service.proxy['after_exit'] = 1
        assert 'blocked' not in service.proxy
        assert service.proxy['after_exit'] == 1
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(2)


def test_dedup_and_revoked_queued_session(service):
    worker = SimpleNamespace()
    # Worker objects in production are identity-hashable.
    class Worker:
        session_id = 'session'
        expected_host = {'generation': 4}
    worker = Worker()
    descriptor = service.attach_session(worker)
    replies = []
    message = dict(rpc_id=1, binding=descriptor['binding'], values={'one': 1})
    service.receive(worker, message, replies.append)
    deadline = time.monotonic() + 2
    while not replies and time.monotonic() < deadline:
        time.sleep(.005)
    assert len(replies) == 1
    service.receive(worker, message, replies.append)
    assert replies[0] == replies[1]
    assert service.proxy.snapshot()[0] == replies[0]['version']
    fd = os.open(service.descriptor['lock_path'], os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_SH)
    try:
        service.receive(worker, dict(message, rpc_id=2, values={'late': 2}), replies.append)
        service.revoke(worker)
    finally:
        os.close(fd)
    deadline = time.monotonic() + 2
    while len(replies) < 3 and time.monotonic() < deadline:
        time.sleep(.005)
    assert 'error' in replies[-1] and 'late' not in service.proxy


def test_corruption_is_error_not_partial_dict(service):
    service.proxy['payload'] = 'value'
    from rc_policy_sdk.global_state import _IDENTITY, _SLOT
    version, _ = service.proxy.snapshot()
    offset = _IDENTITY.size + version % 2 * (_SLOT.size + service.max_bytes) + _SLOT.size
    service.memory.mapping[offset] ^= 1
    reader = SharedState.attach(service.descriptor)
    try:
        with pytest.raises(ValueError, match='checksum'):
            reader.snapshot()
    finally:
        reader.close()


def test_save_failure_or_deadline_preserves_previous_file(tmp_path):
    path = tmp_path / 'global.env'
    path.write_text('old=1\n')
    with pytest.raises(TimeoutError):
        save_state(path, {'new': 2}, time.monotonic())
    assert read_state(path, 1024) == {'old': 1}
    with pytest.raises(RuntimeError):
        save_state(tmp_path / 'missing' / 'global.env', {'new': 2}, time.monotonic() + 2)
    assert read_state(path, 1024) == {'old': 1}


def test_slow_persistence_child_is_killed_within_budget(tmp_path, monkeypatch):
    import robot_control.global_state_file as module
    spawn = subprocess.Popen
    children = []
    def slow(*args, **kwargs):
        child = spawn([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(module.subprocess, 'Popen', slow)
    path = tmp_path / 'global.env'
    path.write_text('old=1\n')
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        save_state(path, {'new': 2}, start + .5)
    assert time.monotonic() - start < .8
    assert children[0].poll() is not None
    assert path.read_text() == 'old=1\n'
