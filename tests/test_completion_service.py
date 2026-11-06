"""Background completion never performs I/O on the calling/UI thread."""
import threading
import time
from types import SimpleNamespace

from robot_control.completion_service import CompletionService


def until(predicate, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(.005)
    raise AssertionError('completion condition timed out')


class Worker:
    def __init__(self, handler=None):
        self.context_id, self.config_version = 'ctx1', 1
        self.session_id, self.runtime_id = 'session', 'runtime'
        self.host = SimpleNamespace(instance_id='host1')
        self.calls = []
        self.handler = handler

    def complete_path(self, selector, prefix, timeout=1):
        self.calls.append((selector, prefix, threading.get_ident()))
        if self.handler:
            return self.handler(prefix)
        return {'items': [{'text': prefix + '.json', 'name': prefix + '.json', 'kind': 'file'}]}


def catalog(worker):
    return {'instances': {'example-1': {'worker': worker, 'commands': {
        'model': {'parameters': [{'name': 'file', 'type': 'path'}]},
    }}}, 'sources': ['example']}


def test_request_is_immediate_and_path_queries_are_debounced_off_ui_thread():
    worker = Worker()
    service = CompletionService(None, catalog=catalog(worker), debounce=.04)
    try:
        line = 'policy example-1 model a'
        started = time.monotonic()
        result = service.request(line, len(line))
        assert time.monotonic() - started < .03
        assert result.path_request and worker.calls == []
        line = 'policy example-1 model ab'
        service.request(line, len(line))
        result = until(lambda: service.poll())
        assert result.text == line
        assert [item.insert_text for item in result.items] == ['ab.json']
        assert len(worker.calls) == 1 and worker.calls[0][1] == 'ab'
        assert worker.calls[0][2] != threading.get_ident()
    finally:
        service.close()


def test_late_response_is_dropped_and_each_instance_has_only_one_active_query():
    entered, release = threading.Event(), threading.Event()
    def slow(prefix):
        if prefix == 'a':
            entered.set()
            release.wait(2)
        return {'items': [{'text': prefix + '.json', 'name': prefix, 'kind': 'file'}]}
    worker = Worker(slow)
    service = CompletionService(None, catalog=catalog(worker), debounce=.005)
    try:
        line = 'policy example-1 model a'
        service.request(line, len(line))
        assert entered.wait(1)
        for prefix in ('ab', 'abc'):
            line = 'policy example-1 model ' + prefix
            service.request(line, len(line))
        time.sleep(.03)
        assert len(worker.calls) == 1
        release.set()
        result = until(lambda: service.poll())
        assert result.text == line and result.items[0].insert_text == 'abc.json'
        assert [call[1] for call in worker.calls] == ['a', 'abc']
    finally:
        release.set()
        service.close()


def test_path_timeout_is_a_hint_close_is_bounded_and_threads_are_daemon():
    release = threading.Event()
    worker = Worker(lambda _: (release.wait(2), {'items': []})[1])
    service = CompletionService(None, catalog=catalog(worker), debounce=.005, timeout=.04)
    try:
        line = 'policy example-1 model x'
        service.request(line, len(line))
        result = until(lambda: service.poll())
        assert '超时' in result.lint
        started = time.monotonic()
        service.close()
        assert time.monotonic() - started < .05
        assert all(thread.daemon for thread in threading.enumerate() if thread.name.startswith('rc-completion'))
    finally:
        release.set()
        service.close()


def test_cached_response_avoids_requery_and_context_change_invalidates_it():
    worker = Worker()
    service = CompletionService(None, catalog=catalog(worker), debounce=.005)
    try:
        line = 'policy example-1 model cached'
        service.request(line, len(line))
        until(lambda: service.poll())
        result = service.request(line, len(line))
        assert not result.items
        assert until(lambda: service.poll()).items
        assert len(worker.calls) == 1
        worker.context_id = 'ctx2'
        worker.config_version = 2
        result = service.request(line, len(line))
        assert not result.items
        until(lambda: service.poll())
        assert len(worker.calls) == 2
    finally:
        service.close()


def test_context_change_during_query_discards_old_result():
    entered, release = threading.Event(), threading.Event()
    def slow(_):
        entered.set()
        release.wait(2)
        return {'items': [{'text': 'stale', 'name': 'stale', 'kind': 'file'}]}
    worker = Worker(slow)
    service = CompletionService(None, catalog=catalog(worker), debounce=.005)
    try:
        line = 'policy example-1 model a'
        service.request(line, len(line))
        assert entered.wait(1)
        worker.context_id = 'new'
        release.set()
        time.sleep(.06)
        result = service.poll()
        assert result is None or not result.items
    finally:
        release.set()
        service.close()


def test_controller_catalog_snapshot_is_background_only_and_updates_input():
    calls = []
    worker = Worker()
    def snapshot():
        calls.append(threading.get_ident())
        return catalog(worker)
    controller = SimpleNamespace(completion_snapshot=snapshot)
    service = CompletionService(controller, refresh_interval=.02)
    try:
        line = 'policy ex'
        result = service.request(line, len(line))
        if not result.items:
            result = until(lambda: service.poll())
        assert [item.label for item in result.items] == ['example-1']
        assert calls and all(identity != threading.get_ident() for identity in calls)
    finally:
        service.close()


def test_local_pose_files_are_listed_in_background_and_insert_usable_absolute_path(tmp_path):
    (tmp_path / 'demo.json').write_text('{}')
    (tmp_path / 'ignored.txt').write_text('')
    service = CompletionService(None, catalog={'pose_root': str(tmp_path)}, debounce=.005)
    try:
        line = 'pose restore de'
        service.request(line, len(line))
        result = until(lambda: service.poll())
        assert len(result.items) == 1
        assert result.items[0].insert_text == str(tmp_path / 'demo.json')
    finally:
        service.close()


def test_missing_path_capability_and_backend_error_only_add_hints():
    for worker in (object(), Worker(lambda _: (_ for _ in ()).throw(RuntimeError('offline')))):
        service = CompletionService(None, catalog=catalog(worker), debounce=.005)
        try:
            line = 'policy example-1 model path'
            service.request(line, len(line))
            result = until(lambda: service.poll())
            assert not result.items and result.lint
        finally:
            service.close()


def test_memory_snapshot_never_uses_registry_or_lifecycle_io(tmp_path):
    from robot_control.human_commands import HumanCommandSupport
    from robot_control.config import PolicyRecord
    class Controller(HumanCommandSupport):
        pass
    controller = Controller()
    controller._lock = threading.RLock()
    controller._identity = 'example'
    config = {'id': 'example', 'execution': {}, 'params': {}, 'tui_commands': {
        'pick': {'handler': 'pick', 'args': [{'name': 'mode', 'type': 'enum', 'choices': ['a', 'b']}]}}}
    record = PolicyRecord(tmp_path, config)
    def forbidden(*args, **kwargs):
        raise AssertionError('metadata snapshot must not discover or execute')
    controller.registry = SimpleNamespace(_records={'example': record}, get=forbidden, list=forbidden)
    controller._worker = Worker()
    controller.config = {'enabled': True}
    controller.outputs = tmp_path
    snapshot = controller.completion_snapshot()
    assert snapshot['instances']['example']['commands']['pick']['parameters'][0]['choices'] == ['a', 'b']
    assert snapshot['instances']['example']['worker'] is controller._worker
    assert snapshot['pose_root'] == str(tmp_path / 'state_saves')
    snapshot['rc_config']['enabled'] = False
    assert controller.config['enabled'] is True


def test_results_are_capped_and_cached_only_for_one_second():
    worker = Worker(lambda _: {'items': [dict(text=str(i), name=str(i), kind='file') for i in range(250)],
                               'truncated': True})
    service = CompletionService(None, catalog=catalog(worker), debounce=.005, cache_ttl=.04)
    try:
        line = 'policy example-1 model x'
        service.request(line, len(line))
        result = until(lambda: service.poll())
        assert len(result.items) == 200 and '200' in result.hint
        time.sleep(.05)
        assert not service.request(line, len(line)).items
        until(lambda: service.poll())
        assert len(worker.calls) == 2
    finally:
        service.close()


def test_process_exit_does_not_wait_for_blocked_completion_threads(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    root = Path(__file__).resolve().parents[1]
    source = '''
from types import SimpleNamespace
import threading
from robot_control.completion_service import CompletionService
entered = threading.Event()
blocked = threading.Event()
def query(*args, **kwargs):
    entered.set()
    blocked.wait(60)
worker = SimpleNamespace(complete_path=query)
catalog = {'instances': {'p': {'worker': worker, 'commands': {'path': {'parameters': [{'type': 'path'}]}}}}}
service = CompletionService(None, catalog=catalog, debounce=.001)
line = 'policy p path x'
service.request(line, len(line))
assert entered.wait(2)
service.close()
'''
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join((str(root / 'src'), str(root / 'packages/policy-sdk/src'))))
    result = subprocess.run([sys.executable, '-c', source], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=4)
    assert result.returncode == 0, result.stderr


def test_source_discovery_failure_keeps_live_in_memory_metadata():
    def missing():
        raise OSError('source directory temporarily unavailable')
    controller = SimpleNamespace(completion_snapshot=lambda: catalog(Worker()),
                                 registry=SimpleNamespace(list=missing))
    service = CompletionService(controller, refresh_interval=.01)
    try:
        line = 'policy ex'
        result = service.request(line, len(line))
        if not result.items:
            result = until(lambda: service.poll())
        assert [item.label for item in result.items] == ['example-1']
    finally:
        service.close()


def test_blocked_source_discovery_does_not_delay_live_instance_changes():
    entered, release = threading.Event(), threading.Event()
    calls = []
    state = {'instance': 'example-1'}
    def discover():
        calls.append(1)
        entered.set()
        release.wait(3)
        return []
    def snapshot():
        return {'instances': {state['instance']: {'commands': {'inspect': {'parameters': []}}}}}
    controller = SimpleNamespace(completion_snapshot=snapshot, registry=SimpleNamespace(list=discover))
    service = CompletionService(controller, refresh_interval=.01)
    try:
        assert entered.wait(1)
        line = 'policy example-1 ins'
        until(lambda: service.request(line, len(line)).items)
        state['instance'] = 'example-2'
        until(lambda: not service.request(line, len(line)).items)
        line = 'policy example-2 ins'
        assert [item.label for item in service.request(line, len(line)).items] == ['inspect']
        assert calls == [1]  # A slow scan never creates replacement scan threads.
    finally:
        service.close()
        release.set()


def test_same_prefix_new_request_reuses_active_result_without_an_extra_query():
    entered, release = threading.Event(), threading.Event()
    def query(prefix):
        entered.set()
        release.wait(2)
        return {'items': [{'text': prefix + '.json', 'kind': 'file'}]}
    worker = Worker(query)
    service = CompletionService(None, catalog=catalog(worker), debounce=.005)
    try:
        line = 'policy example-1 model same'
        service.request(line, len(line))
        assert entered.wait(1)
        latest = service.request(line, len(line))
        release.set()
        result = until(lambda: service.poll())
        assert result.revision == latest.revision
        assert result.items[0].insert_text == 'same.json'
        assert len(worker.calls) == 1
    finally:
        release.set()
        service.close()


def test_cached_old_worker_result_is_not_shown_after_controller_replacement():
    old, new = Worker(), Worker()
    state = {'worker': old}
    controller = SimpleNamespace(completion_snapshot=lambda: catalog(state['worker']))
    service = CompletionService(controller, catalog=catalog(old), debounce=.005, refresh_interval=5)
    try:
        line = 'policy example-1 model old'
        service.request(line, len(line))
        until(lambda: service.poll())
        state['worker'] = new
        result = service.request(line, len(line))
        assert not result.items
        time.sleep(.04)
        result = service.poll()
        assert result is None or not result.items
    finally:
        service.close()


def test_cache_hits_do_not_extend_directory_freshness_forever():
    worker = Worker()
    service = CompletionService(None, catalog=catalog(worker), debounce=.001, cache_ttl=.07)
    try:
        line = 'policy example-1 model item'
        service.request(line, len(line))
        until(lambda: service.poll())
        time.sleep(.04)
        service.request(line, len(line))
        until(lambda: service.poll())
        assert len(worker.calls) == 1
        time.sleep(.04)
        service.request(line, len(line))
        until(lambda: service.poll())
        assert len(worker.calls) == 2
    finally:
        service.close()
