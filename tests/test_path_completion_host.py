"""Filesystem advice must remain bounded and separate from policy execution."""
import os
import threading
import time
from pathlib import Path

import pytest


def tree(tmp_path):
    root = tmp_path / 'models'
    root.mkdir()
    (root / 'z-dir').mkdir()
    (root / 'alpha.pt').write_text('do not read contents')
    (root / 'other.bin').write_text('private model bytes')
    (root / '.hidden.pt').touch()
    return root


def test_paths_are_root_prefixed_directory_first_and_suffix_filtered(tmp_path):
    from rc_policy_sdk.path_completion import list_paths
    tree(tmp_path)
    spec = {'type': 'path', 'root': 'models', 'kind': 'file', 'suffixes': ['.pt']}
    result = list_paths(spec, '', tmp_path)
    assert result['error'] is None
    assert result['items'] == [
        {'name': 'z-dir', 'kind': 'directory', 'text': 'models/z-dir/'},
        {'name': 'alpha.pt', 'kind': 'file', 'text': 'models/alpha.pt'},
    ]
    assert list_paths(spec, 'models/a', tmp_path)['items'][0]['text'] == 'models/alpha.pt'
    assert list_paths(spec, 'models/.', tmp_path)['items'][0]['name'] == '.hidden.pt'
    assert list_paths({**spec, 'root': str(tmp_path / 'models')}, '', tmp_path)['items'][0]['text'] == str(tmp_path / 'models/z-dir') + '/'


def test_paths_reject_parent_and_symlink_escapes_without_reading_content(tmp_path):
    from rc_policy_sdk.path_completion import list_paths
    root = tree(tmp_path)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.pt').touch()
    (root / 'escape').symlink_to(outside, target_is_directory=True)
    spec = {'type': 'path', 'root': 'models'}
    assert list_paths(spec, '../outside/', tmp_path)['error']
    assert list_paths(spec, 'models/escape/', tmp_path)['error']
    assert 'escape' not in [item['name'] for item in list_paths(spec, '', tmp_path)['items']]
    assert list_paths(spec, 'models/missing/', tmp_path)['error']


def test_path_scan_and_output_are_bounded(tmp_path):
    from rc_policy_sdk.path_completion import list_paths
    root = tmp_path / 'models'
    root.mkdir()
    for index in range(230): (root / f'{index:03}.pt').touch()
    result = list_paths({'type': 'path', 'root': 'models'}, '', tmp_path)
    assert len(result['items']) == 200 and result['truncated'] is True
    limited = list_paths({'type': 'path', 'root': 'models'}, '', tmp_path, limit=4)
    assert len(limited['items']) == 4 and limited['truncated'] is True
    assert list_paths({'type': 'path', 'root': 'models'}, '', tmp_path, timeout=0)['error']


SOURCE = '''
import time
def initialize(config): return config
def policy(context, state, action_chunk, images, config): return [[0] * 14]
def reset_context(context): pass
def release(context): pass
def busy(context): time.sleep(.3); return 'done'
def choose(context, path): return path
'''


from contextlib import contextmanager


@contextmanager
def running_host(tmp_path, identity='example-0'):
    import socket
    import tempfile
    from rc_policy_sdk.host import PolicyHost
    from robot_control.process import PolicyProcess
    folder = tmp_path / 'example'
    folder.mkdir(exist_ok=True)
    (folder / 'main.py').write_text(SOURCE)
    if not (folder / 'models').exists(): tree(folder)
    settings = {'schema_version': 3, 'id': identity, 'execution': {'mode': 'sync', 'control_mode': 'qpos', 'call_every_actions': 1},
                'params': {}, 'tui_commands': {'busy': {'handler': 'busy', 'args': []}, 'choose': {'handler': 'choose', 'args': [{'name': 'model', 'type': 'path', 'root': 'models', 'suffixes': ['.pt']}]}},
                'config_hints': {'settings': {'weights': {'type': 'path', 'root': 'models', 'kind': 'file', 'suffixes': ['.pt']}}}}
    host = PolicyHost(tmp_path, identity, source_policy_id='example')
    stop = threading.Event()
    with tempfile.TemporaryDirectory(prefix='rc-path-', dir='/tmp') as socket_dir:
        path = str(Path(socket_dir) / 'host.sock')
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(path)
        listener.listen()
        listener.settimeout(.05)
        def serve():
            while not stop.is_set():
                try: connection, _ = listener.accept()
                except socket.timeout: continue
                except OSError: return
                threading.Thread(target=host.serve_peer, args=(connection,), daemon=True).start()
        acceptor = threading.Thread(target=serve, daemon=True)
        acceptor.start()
        proc = PolicyProcess(folder, settings, host_socket=path, init_timeout_s=5, inference_timeout_s=3, release_timeout_s=2)
        try:
            proc.start()
            yield host, proc
        finally:
            proc.close()
            host.shutting_down = True
            stop.set()
            listener.close()
            acceptor.join(1)


def test_live_path_lookup_is_readonly_during_busy_policy_and_uses_cold_metadata(tmp_path):
    with running_host(tmp_path) as (host, proc):
        assert proc.host.list_paths is True
        proc.submit_command('busy-call', 'busy', [])
        # No module re-read/import, and no on-disk hint change participates in lookup.
        (proc.policy_dir / 'main.py').write_text('raise RuntimeError("must not import")')
        version, context, pid = proc.config_version, proc.context_id, proc.pid
        result = proc.complete_path({'setting': 'weights'}, 'models/a')
        assert result['error'] is None
        assert result['items'] == [{'name': 'alpha.pt', 'kind': 'file', 'text': 'models/alpha.pt'}]
        assert proc.complete_path({'command': 'choose', 'argument': 0}, 'models/a')['items'] == result['items']
        assert (proc.config_version, proc.context_id, proc.pid) == (version, context, pid)
        assert proc.runnable and not proc._commands_blocked
        denied = proc.complete_path({'setting': 'weights', 'root': '/tmp'}, '')
        assert denied['error'] and proc.runnable


def test_slow_lookup_does_not_block_ping_or_stop_and_late_result_is_discarded(tmp_path, monkeypatch):
    import rc_policy_sdk.host as host_module
    entered, release = threading.Event(), threading.Event()
    original = host_module.list_paths
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(host_module, 'list_paths', slow)
    with running_host(tmp_path) as (host, proc):
        answer = []
        thread = threading.Thread(target=lambda: answer.append(proc.complete_path({'setting': 'weights'}, '', timeout=.1)))
        thread.start()
        try:
            assert entered.wait(2)
            before = time.monotonic()
            assert proc.host.request('ping', timeout=.5)['healthy']
            assert time.monotonic() - before < .5
            thread.join(1)
            assert answer and answer[0]['error']
            assert proc.runnable and proc._failure is None
            client = proc.host
            release.set()
            time.sleep(.1)
            assert client.results == {}
            # A stalled new lookup must not participate in shutdown's absolute deadline.
            release.clear(); entered.clear()
            thread = threading.Thread(target=lambda: proc.complete_path({'setting': 'weights'}, 'models/z', timeout=.1))
            thread.start()
            assert entered.wait(2)
            before = time.monotonic()
            proc.close()
            assert proc.cleanup_confirmed and time.monotonic() - before < 2
        finally:
            release.set()
            thread.join(2)


def test_duplicate_source_hosts_require_exact_instance_and_session(tmp_path):
    first = tmp_path / 'one'; first.mkdir()
    second = tmp_path / 'two'; second.mkdir()
    with running_host(first, 'example-0') as (host0, proc0), running_host(second, 'example-1') as (host1, proc1):
        (second / 'example/models/second.pt').touch()
        assert proc1.complete_path({'setting': 'weights'}, 'models/s')['items'][0]['name'] == 'second.pt'
        assert proc0.complete_path({'setting': 'weights'}, 'models/s')['items'] == []
        invalid = proc0.host.request('list_paths', timeout=1, host_id='example-1', host_instance_id=proc0.host.instance_id,
                                    runtime_id=proc1.runtime_id, session_id=proc1.session_id, selector={'setting': 'weights'}, prefix='')
        assert invalid['error'] and proc0.runnable and proc1.runnable


def test_cache_and_latest_pending_query_are_bounded_and_nonfatal(tmp_path, monkeypatch):
    import rc_policy_sdk.host as host_module
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = host_module.list_paths
    def controlled(spec, prefix, *args, **kwargs):
        calls.append(prefix)
        if prefix == 'models/z':
            entered.set()
            assert release.wait(3)
        return original(spec, prefix, *args, **kwargs)
    monkeypatch.setattr(host_module, 'list_paths', controlled)
    with running_host(tmp_path) as (host, proc):
        assert not proc.complete_path({'setting': 'weights'}, '')['error']
        assert not proc.complete_path({'setting': 'weights'}, '')['error']
        assert calls == ['']
        responses, errors, threads = {}, {}, []
        def query(prefix):
            try:
                responses[prefix] = proc.host.request('list_paths', timeout=.5, host_id=proc.config['id'],
                    host_instance_id=proc.host.instance_id, runtime_id=proc.runtime_id, session_id=proc.session_id,
                    selector={'setting': 'weights'}, prefix=prefix)
            except TimeoutError as exc: errors[prefix] = str(exc)
        def start(prefix):
            thread = threading.Thread(target=query, args=(prefix,))
            threads.append(thread); thread.start()
        def pending(prefix):
            deadline = time.monotonic() + 2
            with host._path_condition:
                while host._path_pending is None or host._path_pending[1]['prefix'] != prefix:
                    assert time.monotonic() < deadline
                    host._path_condition.wait(.01)
        try:
            start('models/z')
            assert entered.wait(2)
            start('models/a'); pending('models/a')
            start('models/.'); pending('models/.')
            release.set()
            for thread in threads: thread.join(2)
            assert calls == ['', 'models/z', 'models/.']
            assert responses['models/.']['items'][0]['name'] == '.hidden.pt'
            assert 'models/a' in errors
            assert proc.host.results == {} and proc.runnable
        finally:
            release.set()
            for thread in threads: thread.join(2)


def test_changed_context_and_old_host_are_advice_only(tmp_path, monkeypatch):
    import rc_policy_sdk.host as host_module
    entered, release = threading.Event(), threading.Event()
    original = host_module.list_paths
    def delayed(*args, **kwargs):
        entered.set(); assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(host_module, 'list_paths', delayed)
    with running_host(tmp_path) as (host, proc):
        answers = []
        thread = threading.Thread(target=lambda: answers.append(proc.complete_path({'setting': 'weights'}, '')))
        thread.start()
        try:
            assert entered.wait(2)
            proc.context_id = 'replacement-context'
            release.set(); thread.join(2)
            assert 'stale' in answers[0]['error'] and proc.runnable
            proc.host.list_paths = False
            assert 'support' in proc.complete_path({'setting': 'weights'}, '')['error']
            assert proc.runnable and proc._failure is None
        finally:
            release.set(); thread.join(2)


def test_declared_relative_root_always_produces_relative_insertions(tmp_path):
    from rc_policy_sdk.path_completion import list_paths
    root = tree(tmp_path)
    result = list_paths({'type': 'path', 'root': 'models'}, str(root / 'a'), tmp_path)
    assert result['items'][0]['text'] == 'models/alpha.pt'


@pytest.mark.parametrize('absolute', [False, True])
def test_partially_typed_declared_root_can_be_navigated(tmp_path, absolute):
    from rc_policy_sdk.path_completion import list_paths
    root = tree(tmp_path)
    declared = str(root) if absolute else 'models'
    result = list_paths({'type': 'path', 'root': declared}, declared[:-2], tmp_path)
    assert result['items'] == [{'name': 'models', 'kind': 'directory', 'text': declared + '/'}]


def test_slow_socket_keeps_only_latest_path_reply_behind_control_messages():
    import json
    from rc_policy_sdk.host import Peer
    entered, release = threading.Event(), threading.Event()
    packets = []
    class SlowSocket:
        def sendall(self, packet):
            if not packets:
                entered.set()
                assert release.wait(3)
            packets.append(json.loads(packet[4:]))
        def shutdown(self, how): pass
    peer = Peer(SlowSocket())
    try:
        peer.send({'type': 'rpc_result', 'rpc_id': 0, 'result': {'control': 'active'}})
        assert entered.wait(1)
        for identity in range(1, 101):
            peer.send_path_result({'type': 'rpc_result', 'rpc_id': identity, 'result': {'items': []}})
        peer.send({'type': 'rpc_result', 'rpc_id': 200, 'result': {'control': 'stop'}})
        assert peer.outbox.qsize() == 2
        release.set()
        deadline = time.monotonic() + 2
        while len(packets) < 3 and time.monotonic() < deadline: time.sleep(.005)
        assert [packet['rpc_id'] for packet in packets] == [0, 200, 100]
    finally:
        release.set()
        peer.close()


def test_client_bounds_pending_completion_rpcs_and_drops_superseded_requests(tmp_path, monkeypatch):
    import rc_policy_sdk.host as host_module
    entered, release = threading.Event(), threading.Event()
    original = host_module.list_paths
    def slow(*args, **kwargs):
        entered.set(); assert release.wait(4)
        return original(*args, **kwargs)
    monkeypatch.setattr(host_module, 'list_paths', slow)
    with running_host(tmp_path) as (host, proc):
        errors, threads = [], []
        def request(index):
            try:
                proc.host.request('list_paths', timeout=2, host_id=proc.config['id'], host_instance_id=proc.host.instance_id,
                    runtime_id=proc.runtime_id, session_id=proc.session_id, selector={'setting': 'weights'}, prefix=str(index))
            except (TimeoutError, RuntimeError) as exc: errors.append(str(exc))
        try:
            for index in range(20):
                thread = threading.Thread(target=request, args=(index,)); threads.append(thread); thread.start()
            assert entered.wait(2)
            time.sleep(.1)
            with proc.host.condition:
                assert len(proc.host._pending_rpcs) <= 2
            assert proc.host.outbox.qsize() <= 1
        finally:
            release.set()
            for thread in threads: thread.join(3)
        assert all(not thread.is_alive() for thread in threads)
        assert proc.host.results == {} and proc._failure is None and proc.runnable


def test_path_advice_filters_terminal_controls_but_keeps_ordinary_unicode(tmp_path):
    from rc_policy_sdk.path_completion import list_paths
    root = tree(tmp_path)
    for name in ('bad\nname.pt', 'bad\x1b[31m.pt', 'bad\x7f.pt', '带 空格"和\\斜线.pt'):
        (root / name).touch()
    result = list_paths({'type': 'path', 'root': 'models'}, '', tmp_path)
    names = [item['name'] for item in result['items']]
    assert '带 空格"和\\斜线.pt' in names
    assert not any(any(ord(char) < 32 or ord(char) == 127 for char in name) for name in names)
    failure = list_paths({'type': 'path', 'root': 'missing\x1b[31m'}, '', tmp_path)
    assert failure['error'] and '\x1b' not in failure['error']


@pytest.mark.parametrize('field', ['host_id', 'host_instance_id', 'runtime_id', 'session_id'])
def test_each_path_request_identity_is_checked_independently(tmp_path, field):
    with running_host(tmp_path) as (host, proc):
        identity = dict(host_id=proc.config['id'], host_instance_id=proc.host.instance_id,
                        runtime_id=proc.runtime_id, session_id=proc.session_id)
        identity[field] = 'stale'
        answer = proc.host.request('list_paths', timeout=1, **identity, selector={'setting': 'weights'}, prefix='')
        assert answer['error'] and answer['items'] == []
        assert proc.runnable and proc._failure is None


def test_path_owner_cannot_borrow_another_connections_live_session(tmp_path):
    from robot_control.host_client import HostClient
    with running_host(tmp_path) as (host, proc):
        other = HostClient(proc.host_socket, proc.config['id'])
        try:
            answer = other.request('list_paths', timeout=1, host_id=proc.config['id'], host_instance_id=proc.host.instance_id,
                                   runtime_id=proc.runtime_id, session_id=proc.session_id, selector={'setting': 'weights'}, prefix='')
            assert answer['error'] and not answer['items']
            assert proc.runnable
        finally:
            other.disconnect()


def test_path_cache_expires_after_one_second(tmp_path):
    with running_host(tmp_path) as (host, proc):
        first = proc.complete_path({'setting': 'weights'}, '')
        (proc.policy_dir / 'models/new.pt').touch()
        assert proc.complete_path({'setting': 'weights'}, '') == first
        time.sleep(1.05)
        assert 'new.pt' in [item['name'] for item in proc.complete_path({'setting': 'weights'}, '')['items']]
