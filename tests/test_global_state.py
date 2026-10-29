import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from rc_policy_sdk.global_state import GlobalStateConflict, SharedState, validate_values
from robot_control.global_state import GlobalStateService
from robot_control.global_state_file import read_state, save_state


def test_shared_snapshot_and_atomic_updates(tmp_path):
    service = GlobalStateService(tmp_path / 'global.env', tmp_path / 'ipc', run_id='run')
    service.start()
    reader = SharedState.attach(service.descriptor)
    try:
        g = service.proxy
        g.update({'task': {'phase': '抓取'}, 'ready': True, 'items': [1, None, 1.5]})
        version, data = reader.snapshot()
        assert data == {'task': {'phase': '抓取'}, 'ready': True, 'items': [1, None, 1.5]}
        data['task']['phase'] = 'local'
        assert g['task']['phase'] == '抓取'
        assert g.patch({'ready': False}, delete=('items',), expected_version=version) == version + 1
        with pytest.raises(GlobalStateConflict):
            g.patch({'lost': True}, expected_version=version)
        assert 'lost' not in g and 'items' not in g
        with pytest.raises(KeyError):
            g.patch({'partial': 1}, delete=('missing',))
        assert 'partial' not in g
        with pytest.raises(TypeError):
            reader.mapping[0] = 1
    finally:
        reader.close()
        service.close(time.monotonic() + 3)


def test_concurrent_disjoint_and_conditional_updates(tmp_path):
    service = GlobalStateService(tmp_path / 'global.env', tmp_path / 'ipc', run_id='run')
    service.start()
    try:
        g = service.proxy
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda n: g.update({f'key_{n}': n}), range(40)))
        assert len(g) == 40
        g['counter'] = 0
        def increment(_):
            for attempt in range(100):
                version, data = g.snapshot()
                try:
                    g.patch({'counter': data['counter'] + 1}, expected_version=version)
                    return
                except GlobalStateConflict:
                    pass
            raise AssertionError('CAS did not converge')
        with ThreadPoolExecutor(8) as pool:
            list(pool.map(increment, range(40)))
        assert g['counter'] == 40
    finally:
        service.close(time.monotonic() + 3)


@pytest.mark.parametrize('value', [{'a': float('nan')}, {'a': (1, 2)}, {'a': {1: 'x'}}, {'bad.key': 1}, {'a': object()}])
def test_only_strict_json_and_env_keys(value):
    with pytest.raises((TypeError, ValueError)):
        validate_values(value)


def test_capacity_failure_preserves_version(tmp_path):
    service = GlobalStateService(tmp_path / 'global.env', tmp_path / 'ipc', run_id='run', max_bytes=64)
    service.start()
    try:
        before = service.proxy.snapshot()
        with pytest.raises(ValueError, match='capacity'):
            service.proxy['too_large'] = 'x' * 100
        assert service.proxy.snapshot() == before
    finally:
        service.close(time.monotonic() + 3)


def test_file_roundtrip_and_invalid_file_preserved(tmp_path):
    path = tmp_path / 'global.env'
    assert read_state(path, 1024) == {}
    path.write_text('# init\ntext="a=b # $HOME\\n中文"\ncount=2\nitems=[null,true]\n')
    data = read_state(path, 1024)
    save_state(path, data, time.monotonic() + 3)
    assert read_state(path, 1024) == data
    for invalid in ['a=1\na=2\n', 'a=NaN\n', 'a={"x":1,"x":2}\n', 'a=unquoted\n']:
        path.write_text(invalid)
        with pytest.raises(ValueError):
            read_state(path, 1024)
        assert path.read_text() == invalid


def test_freeze_saves_and_rejects_later_writes(tmp_path):
    path = tmp_path / 'global.env'
    service = GlobalStateService(path, tmp_path / 'ipc', run_id='run')
    service.start()
    service.proxy['final'] = 42
    service.finish(time.monotonic() + 3)
    assert read_state(path, 1024) == {'final': 42}
    with pytest.raises(RuntimeError):
        service.proxy['late'] = True
    service.close(time.monotonic() + 3)
