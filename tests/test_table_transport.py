"""Real worker transport and RC identity/redaction boundaries for table output."""
import copy
import json
from pathlib import Path

import pytest

from host_helpers import LocalPolicyProcess
from test_host_transport import wait_for
from test_command_worker import wait_commands


SOURCE = '''
from rc_policy_sdk import Table, TableOutput
old_runtime = None
def output(value):
    return TableOutput(result={'value': value}, tables=[Table(columns=['value'], rows=[[value]])])
def initialize(config, runtime):
    global old_runtime
    if old_runtime is not None:
        try: old_runtime.log_table(output('STALE'))
        except RuntimeError: pass
        else: raise ValueError('old context accepted')
    runtime.log_table(output('initialize'))
    old_runtime = runtime
    if config['params'].get('fail'): raise ValueError('initialization failed')
    return {'runtime': runtime, 'config': config}
def policy(context, state, action_chunk, images, config, runtime):
    runtime.log_table(output('policy'))
    return [[0] * 14]
def observe(context, state, images, config, runtime):
    runtime.log_table(output('observe'))
def ping(context):
    context['runtime'].log_table(output(context['config']['params']['value']))
    return output(False)
def plain(context): return {'table':False,'tables':0,'table_document':None}
def wrapped(context): return output(None)
def configure(context, config, key, value, callback):
    config['params']['value'] = int(value)
def reset_context(context): pass
def release(context): pass
'''


def make_worker(tmp_path, *, identity='tables-0', events=None, fail=False, source=SOURCE):
    folder = tmp_path / 'tables'
    folder.mkdir(exist_ok=True)
    (folder / 'main.py').write_text(source)
    config = {'schema_version': 3, 'id': identity, 'params': {'value': 1, 'fail': fail},
              'hot_reloadable': ['params.value'],
              'execution': {'mode': 'sync', 'call_every_actions': 1, 'control_mode': 'qpos'},
              'tui_commands': {name: {'handler': name, 'args': []} for name in ('ping', 'plain', 'wrapped')}}
    return LocalPolicyProcess(folder, config, on_log_event=(events if events is not None else []).append,
                              init_timeout_s=5, inference_timeout_s=3, release_timeout_s=1)


@pytest.fixture
def images(tmp_path):
    import numpy as np
    from rc_policy_sdk.images import ImagePool
    pool = ImagePool(tmp_path / 'frames', cameras=('head',))
    pool.publish('head', np.zeros((2, 2, 3), np.uint8), 1.)
    lease = pool.acquire()
    try:
        yield lease.descriptors
    finally:
        lease.release()
        pool.close()


def test_initialize_policy_observe_and_saved_runtime_versions(tmp_path, images):
    events = []
    worker = make_worker(tmp_path, events=events)
    try:
        worker.start()
        assert worker.supports_table_output
        wait_for(lambda: len(events) == 1)
        assert events[0]['message'] == 'value\ninitialize'
        assert events[0]['event'] == 'policy.log'
        assert events[0]['instance_id'] == 'tables-0' and events[0]['source_id'] == 'tables'
        request = dict(request_id=4, generation=8, config_version=1, state={}, action_chunk=[], images=images)
        worker.submit(request)
        response = wait_for(worker.poll)
        assert response['type'] == 'result', response.get('error', response)
        worker.observe({**request, 'request_id': 5})
        assert wait_for(worker.poll_observation)['type'] == 'observed'
        wait_for(lambda: len(events) == 3)
        assert [(e.get('request_id'), e.get('generation')) for e in events[1:]] == [(4, 8), (5, 8)]
        worker.update_config({'params.value': 2}, 2)
        worker.submit_command('hot', 'ping', [])
        response = wait_commands(worker, 1)['hot']
        assert response['result'] == {'value': False}
        assert response['table_document']['tables'][0]['rows'] == [[False]]
        wait_for(lambda: len(events) == 4)
        assert events[-1]['config_version'] == 2
        assert json.loads(events[-1]['copy_json']) == {'value': 2}
        prepared = worker.prepare_config('params.value', '3')
        worker.apply_config(prepared['config'], 3, prepared['action'])
        worker.commit_config(prepared['config'], 3)
        worker.publish_config(prepared['config'], 3)
        worker.resume_commands()
        worker.submit_command('configured', 'ping', [])
        assert wait_commands(worker, 1)['configured']['type'] == 'command_result'
        wait_for(lambda: len(events) == 5)
        assert events[-1]['config_version'] == 3
        old_context = worker.context_id
        candidate = copy.deepcopy(worker.config)
        candidate['params']['value'] = 4
        worker.release_context()
        flags = worker.initialize_context(candidate, 4)
        # initialize log is valid before publication of the next config version.
        wait_for(lambda: len(events) == 6)
        assert events[-1]['context_id'] != old_context
        assert events[-1]['config_version'] == 4
        worker.commit_reinitialize(candidate, 4, flags)
        worker.resume_commands()
        before = len(events)
        for runtime_id, context_id in [('old-runtime', worker.context_id), (worker.runtime_id, old_context)]:
            worker._receive(dict(runtime_id=runtime_id, session_id=worker.session_id, event={
                'type': 'log', 'context_id': context_id, 'config_version': 4,
                'message': 'forged', 'log_event': 'sdk.log_gap', 'count': 1}))
        assert len(events) == before
        worker._receive(dict(runtime_id=worker.runtime_id, session_id=worker.session_id, event={
            'type': 'log', 'context_id': worker.context_id, 'config_version': 3,
            'message': 'stale version', 'log_event': 'sdk.log_gap', 'count': 1}))
        assert len(events) == before
        assert all('STALE' not in event['message'] for event in events)
    finally:
        worker.close()


def test_failed_initialization_table_diagnostic_survives_control_error(tmp_path):
    events = []
    worker = make_worker(tmp_path, events=events, fail=True)
    try:
        with pytest.raises(RuntimeError, match='initialization failed'):
            worker.start()
        wait_for(lambda: events)
        assert events[0]['message'] == 'value\ninitialize'
    finally:
        worker.close()


def test_failed_reinitialization_keeps_new_context_diagnostics_after_revoke(tmp_path):
    events = []
    worker = make_worker(tmp_path, events=events)
    try:
        worker.start()
        wait_for(lambda: len(events) == 1)
        candidate = copy.deepcopy(worker.config)
        candidate['params']['fail'] = True
        worker.release_context()
        with pytest.raises(RuntimeError, match='initialization failed'):
            worker.initialize_context(candidate, 2)
        wait_for(lambda: len(events) == 2)
        assert events[-1]['config_version'] == 2
        assert events[-1]['context_id'] == worker.context_id
        # A retained low-priority packet can arrive after the control error.
        worker._receive(dict(runtime_id=worker.runtime_id, session_id=worker.session_id, event={
            'type': 'log', 'context_id': worker.context_id, 'config_version': 2,
            'message': 'late failure diagnostic', 'log_event': 'sdk.log_gap', 'count': 1}))
        assert events[-1]['message'] == 'late failure diagnostic'
    finally:
        worker.close()


def test_missing_host_capability_rejects_wrappers_but_keeps_ordinary_json(tmp_path):
    from robot_control.host_client import HostClient
    worker = make_worker(tmp_path, source=SOURCE.replace("runtime.log_table(output('initialize'))", 'pass'))
    host = HostClient.get(worker.host_socket, worker.config['id'])
    host.table_output_v1 = False
    try:
        worker.start()
        assert not worker.supports_table_output
        worker.submit_command('plain', 'plain', [])
        assert wait_commands(worker, 1)['plain']['result'] == {'table':False,'tables':0,'table_document':None}
        for name in ('wrapped', 'ping'):
            worker.submit_command(name, name, [])
            result = wait_commands(worker, 1)[name]
            assert result['type'] == 'command_error'
            assert 'upgrade robot-control' in result['error']
            assert 'table_document' not in result
    finally:
        worker.close()


def test_repeated_source_chain_preserves_tables_and_actual_instance_identity(tmp_path, images):
    from robot_control.config import PolicyRegistry
    from robot_control.chain_process import ChainProcess
    from test_chain_config import add_chain, add_policy
    config = add_policy(tmp_path, 'tables', commands={name: {'handler':name,'args':[]} for name in ('ping','plain','wrapped')})
    config['params']['value'] = 1
    (tmp_path/'tables'/'policy.yaml').write_text(json.dumps(config))
    (tmp_path/'tables'/'main.py').write_text(SOURCE)
    add_chain(tmp_path, ['tables', 'tables'])
    record = PolicyRegistry(tmp_path).get('default_chain')
    events = []
    worker = ChainProcess(record, process_factory=LocalPolicyProcess, on_log_event=events.append,
                          init_timeout_s=5, inference_timeout_s=3, release_timeout_s=1)
    try:
        worker.start()
        wait_for(lambda: len(events) == 2)
        assert {e['instance_id'] for e in events} == {'tables-0','tables-1'}
        worker.submit(dict(request_id=1, generation=7, config_version=record.version, state={},
                           action_chunk=[], images=images))
        assert wait_for(worker.poll)['type'] == 'result'
        wait_for(lambda: len(events) == 4)
        assert [e['instance_id'] for e in events[2:]] == ['tables-0', 'tables-1']
        for identity in ('tables-0','tables-1'):
            worker.submit_command(identity, identity+'-wrapped', [])
            response = wait_commands(worker, 1)[identity]
            assert response['result'] == {'value': None}
            assert response['table_log_context']['instance_id'] == identity
            assert response['table_log_context']['source_id'] == 'tables'
            assert response['table_document']['tables'][0]['rows'] == [[None]]
    finally:
        worker.close()


def test_repeated_source_instances_keep_independent_identity(tmp_path):
    events = []
    workers = [make_worker(tmp_path, identity=f'tables-{n}', events=events) for n in range(2)]
    try:
        for worker in workers: worker.start()
        wait_for(lambda: len(events) == 2)
        assert {e['instance_id'] for e in events} == {'tables-0', 'tables-1'}
        assert {e['source_id'] for e in events} == {'tables'}
        assert len({e['runtime_id'] for e in events}) == len({e['session_id'] for e in events}) == 2
    finally:
        for worker in workers: worker.close()


def test_table_redaction_covers_sensitive_columns_field_rows_and_nested_cells():
    from rc_policy_sdk import Table, TableOutput
    from robot_control.table_output import public_table_fields
    from rc_policy_sdk.tables import normalize_table_output
    output = TableOutput(result={'api_key': 'SECRET'}, tables=[
        Table(columns=['api_key', 'ordinary'], rows=[['SECRET', 0]]),
        Table(columns=['fields', 'value'], rows=[['api_key', 'SECRET'], ['count', False]]),
        Table(columns=['项目', '值'], rows=[['api_key', 'SECRET']]),
        Table(columns=['字段', '值'], rows=[['api_key', 'SECRET']]),
        Table(title='结果 SECRET', columns=['显示'], rows=[['前缀 SECRET 后缀']]),
        Table(columns=['path', 'value'], rows=[['provider.api_key', 'SECRET']]),
        Table(columns=['nested'], rows=[[{'api_key': 'SECRET'}], ['{"api_key":"SECRET"}'], [{'SECRET':'safe'}]]),
    ])
    fields = public_table_fields(*normalize_table_output(output))
    assert 'SECRET' not in json.dumps(fields)
    assert json.loads(fields['copy_json']) == {'api_key': '***'}
    assert fields['table_document']['tables'][1]['rows'] == [['api_key', '***'], ['count', False]]
    assert output.result['api_key'] == 'SECRET'


def test_valid_log_identity_with_bad_table_metadata_falls_back_to_raw_json(tmp_path):
    events = []
    worker = make_worker(tmp_path, events=events)
    try:
        worker.start()
        wait_for(lambda: len(events) == 1)
        worker._receive(dict(runtime_id=worker.runtime_id, session_id=worker.session_id, event={
            'type':'log','context_id':worker.context_id,'config_version':1,
            'message':'misleading projection','result':{'api_key':'SECRET','value':False},
            'table_document':{'schema_version':99,'tables':[]}}))
        assert len(events) == 2
        assert events[-1]['message'] == events[-1]['copy_json']
        assert json.loads(events[-1]['copy_json']) == {'api_key':'***','value':False}
        assert 'table_document' not in events[-1]
        assert worker.runnable
    finally:
        worker.close()


def log_receiver():
    from types import SimpleNamespace
    from robot_control.process import PolicyProcess
    from rc_policy_sdk import Table, TableOutput
    from rc_policy_sdk.tables import normalize_table_output
    worker = PolicyProcess('/tmp/tables', {'id':'tables-0','params':{}}, host_socket='/unused')
    worker.host = SimpleNamespace(table_output_v1=True, instance_id='host')
    worker.runtime_id, worker.session_id, worker.context_id = 'runtime', 'session', 'context'
    result, document = normalize_table_output(TableOutput(result={'value':0}, tables=[Table(columns=['value'], rows=[[0]])]))
    packet = {'runtime_id':'runtime','session_id':'session','event':{
        'type':'log','context_id':'context','config_version':1,'result':result,'table_document':document}}
    return worker, packet


@pytest.mark.parametrize('stage', ['sanitize', 'trace', 'publish'])
def test_review_r2_log_publication_and_callbacks_never_hold_receive_condition(monkeypatch, stage):
    import threading
    from robot_control import table_output
    worker, packet = log_receiver()
    entered, release = threading.Event(), threading.Event()
    delivered = []
    def block():
        entered.set()
        assert release.wait(5)
    original = table_output.public_table_fields
    def publishing(*args):
        block()
        return original(*args)
    if stage == 'sanitize': monkeypatch.setattr(table_output, 'public_table_fields', publishing)
    worker.on_trace = (lambda _: block()) if stage == 'trace' else None
    worker.on_log_event = (lambda event: (block(), delivered.append(event))) if stage == 'publish' else delivered.append
    thread = threading.Thread(target=worker._receive, args=(packet,))
    thread.start()
    try:
        assert entered.wait(3)
        acquired = worker._condition.acquire(timeout=1)
        if acquired: worker._condition.release()
        assert acquired, 'publication held the condition used by control/monitor consumers'
    finally:
        release.set()
        thread.join(3)
    assert len(delivered) == 1


@pytest.mark.parametrize('changed', ['context_id', 'runtime_id', 'config_version'])
def test_log_binding_is_rechecked_after_publication_preparation(monkeypatch, changed):
    import threading
    from robot_control import table_output
    worker, packet = log_receiver()
    entered, release = threading.Event(), threading.Event()
    delivered = []
    original = table_output.public_table_fields
    def publishing(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(table_output, 'public_table_fields', publishing)
    worker.on_trace = worker.on_log_event = delivered.append
    thread = threading.Thread(target=worker._receive, args=(packet,))
    thread.start()
    try:
        assert entered.wait(3)
        acquired = worker._condition.acquire(timeout=1)
        assert acquired
        try:
            setattr(worker, changed, 2 if changed == 'config_version' else 'replacement')
        finally:
            worker._condition.release()
    finally:
        release.set()
        thread.join(3)
    assert not delivered
