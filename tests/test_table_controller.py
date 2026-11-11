"""Tables retain their business result and publish after effect completion."""
import io
import json
import time

import pytest

from config_helpers import connector_config
from connector_helpers import MockRobot
from host_helpers import LocalPolicyProcess
from robot_control.controller import RobotController
from robot_control.cli import drain_events
from robot_control.log_format import event_metadata
from test_controller import until
from test_command_controller import completed


SOURCE = '''
import time
from pathlib import Path
from rc_policy_sdk import Table, TableOutput
def report(result):
    return TableOutput(result=result, tables=[Table(title='diagnostic', columns=['api_key','count'], rows=[['SECRET',0]])])
def initialize(config, runtime):
    validate_config(config)
    runtime.log_table(report({'api_key':'SECRET','count':0}))
    return config
def validate_config(config):
    if config['params']['value'] != 1:
        root = Path(config['params']['root'])
        (root/'effect-start').touch()
        deadline = time.monotonic()+5
        while not (root/'effect-release').exists() and time.monotonic() < deadline: time.sleep(.005)
        if config['params']['value'] < 0: raise ValueError('effect failed')
def policy(context, state, action_chunk, images, config): return [[0]*14]
def reset_context(context): pass
def release(context): pass
def wrapped(context): return report({'api_key':'SECRET','count':0})
def plain(context): return {'table': 0, 'tables': False, 'table_document': None}
def change(context, value): return report({'params.value':value})
'''


@pytest.fixture
def table_controller(tmp_path, request):
    effect = getattr(request, 'param', 'update_config')
    folder = tmp_path/'policy'/'tables'
    folder.mkdir(parents=True)
    (folder/'main.py').write_text(SOURCE)
    (folder/'compose.yaml').write_text('services:\n  policy:\n    image: fixture:local\n')
    config = {'schema_version': 3, 'id': 'tables', 'params': {'root':str(tmp_path), 'value':1},
        'execution': {'mode':'sync','call_every_actions':1,'control_mode':'qpos'},
        'hot_reloadable': ['params.value'] if effect == 'update_config' else [],
        'tui_commands': {'wrapped': {'handler':'wrapped','args':[]}, 'plain': {'handler':'plain','args':[]},
                         'change': {'handler':'change','args':['int'],'effect':effect}}}
    (folder/'policy.yaml').write_text(json.dumps(config))
    rc_config = {'schema_version':3, 'frequency_hz':50, 'policy_directory':'policy',
        'startup_policy':'tables','robot':{'connector':'body'},
        'safety':{'enabled':False,'reference_frequency_hz':50,'max_delta_per_step':[.1]*14},
        'reset_qpos':[0]*14,'outputs_directory':'outputs',
        'timeouts':{'initialize_s':10,'inference_s':5,'release_s':1}}
    path = tmp_path/'rc.yaml'
    path.write_text(json.dumps(connector_config(rc_config)))
    rc = RobotController(path, robot=MockRobot(), process_factory=LocalPolicyProcess)
    rc.start()
    try:
        until(lambda: rc.status()['policy']=='tables' and rc.status()['state']=='paused', 15)
        yield rc, tmp_path
    finally:
        (tmp_path/'effect-release').touch()
        if not rc.done.is_set():
            rc.submit('exit')
            assert rc.done.wait(10)


def table_events(rc, command_id=None):
    return [e for e in rc.read_events()['events'] if 'table_document' in e
            and (command_id is None or e.get('command_id') == command_id)]


def test_command_and_active_tables_keep_redacted_copy_and_trusted_source(table_controller):
    rc, root = table_controller
    until(lambda: table_events(rc))
    active = table_events(rc)[0]
    assert active['event'] == 'policy.log'
    assert active['instance_id'] == active['source_id'] == 'tables'
    assert event_metadata(active)[2] == 'policy:tables'
    assert active['message'] == 'diagnostic\napi_key\tcount\n***\t0'
    assert json.loads(active['copy_json']) == {'api_key':'***','count':0}
    op = rc.submit('policy tables wrapped')
    receipt = completed(rc, op)
    assert receipt['result'] == {'api_key':'***','count':0}
    until(lambda: table_events(rc, op))
    command = table_events(rc, op)[0]
    assert command['event'] == 'policy.command'
    assert json.loads(command['copy_json']) == receipt['result']
    assert event_metadata(command)[2] == 'policy:tables'
    plain_op = rc.submit('policy tables plain')
    assert completed(rc, plain_op)['result'] == {'table':0, 'tables':False, 'table_document':None}
    assert not table_events(rc, plain_op)
    text = io.StringIO()
    drain_events(rc, text)
    assert 'SECRET' not in text.getvalue()
    assert 'policy:tables' in text.getvalue()
    assert 'SECRET' not in json.dumps(rc.read_events())
    rc.submit('exit')
    assert rc.done.wait(10)
    logs = (rc.outputs/'logs'/f'rc_{rc.run_id}.jsonl').read_text()
    assert 'SECRET' not in logs
    assert 'copy_json' in logs and 'table_document' in logs


@pytest.mark.parametrize('table_controller', ['update_config','reinitialize_policy','reload_policy','reload_all_policy'], indirect=True)
def test_effect_table_appears_only_after_final_success(table_controller):
    rc, root = table_controller
    op = rc.submit('policy tables change 2')
    until(lambda: (root/'effect-start').exists(), 8)
    assert not table_events(rc, op)
    assert rc.get_operation(op)['status'] == 'running'
    (root/'effect-release').touch()
    receipt = completed(rc, op, timeout=12)
    assert receipt['status'] == 'succeeded', receipt
    until(lambda: table_events(rc, op))
    assert len(table_events(rc, op)) == 1
    assert json.loads(table_events(rc, op)[0]['copy_json']) == {'params.value':2}
    assert rc.registry.get('tables').config['params']['value'] == 2


@pytest.mark.parametrize('outcome', ['failure','cancel'])
@pytest.mark.parametrize('table_controller', ['update_config','reinitialize_policy'], indirect=True)
def test_effect_failure_and_cancellation_never_publish_table(table_controller, outcome):
    rc, root = table_controller
    op = rc.submit('policy tables change ' + ('-1' if outcome == 'failure' else '2'))
    until(lambda: (root/'effect-start').exists(), 8)
    assert not table_events(rc, op)
    if outcome == 'cancel':
        completed(rc, rc.submit('pause'))
    (root/'effect-release').touch()
    receipt = completed(rc, op, timeout=12)
    assert receipt['status'] == ('failed' if outcome == 'failure' else 'cancelled'), receipt
    if outcome == 'failure':
        assert 'effect failed' in receipt['error']
    assert not table_events(rc, op)
    assert not rc._policy_calls and not rc._policy_reloads


@pytest.mark.parametrize('effect', [False, True])
@pytest.mark.parametrize('document', [{'schema_version':99,'tables':[]},
    {'schema_version':1,'tables':[{'title':'bad','columns':['a'],'rows':[[1,2]]}]}])
def test_invalid_display_metadata_preserves_business_result_and_effect(table_controller, monkeypatch, effect, document):
    rc, root = table_controller
    worker = rc._worker
    original = worker._receive_command
    def receive(message):
        if message['type'] == 'command_result':
            message = {**message, 'table_document':document}
        return original(message)
    monkeypatch.setattr(worker, '_receive_command', receive)
    op = rc.submit('policy tables change 2' if effect else 'policy tables wrapped')
    if effect:
        until(lambda: (root/'effect-start').exists(), 5)
        (root/'effect-release').touch()
    receipt = completed(rc, op)
    assert receipt['status'] == 'succeeded', receipt
    if effect:
        assert rc.registry.get('tables').config['params']['value'] == 2
    else:
        assert receipt['result'] == {'api_key':'***','count':0}
        assert any(f'policy command [{op}] succeeded: ' in e['message'] for e in rc.read_events()['events'])
    assert not table_events(rc, op)


def test_emit_ignores_bad_optional_table_metadata(table_controller):
    rc, _ = table_controller
    rc._emit('original message', table_document={'schema_version':99}, copy_json='{"count": 0}')
    event = rc.read_events()['events'][-1]
    assert event['message'] == event['copy_json'] == '{"count": 0}'
    assert 'table_document' not in event


@pytest.mark.parametrize('outcome', ['failed', 'cancelled', 'stopped'])
def test_review_r3_terminal_effect_releases_payload_while_manager_keeps_last_job(outcome):
    import gc
    import queue
    import threading
    import weakref
    from types import SimpleNamespace
    class Fields(dict): pass
    fields = Fields(message='x'*100000, copy_json='{}', table_document={})
    reference = weakref.ref(fields)
    receipts = []
    cancel, stop = threading.Event(), threading.Event()
    if outcome == 'cancelled': cancel.set()
    if outcome == 'stopped': stop.set()
    job = dict(id=1, kind='reload_member', public_id='cmd', old_worker=None,
               prepared=False, cancel=cancel, command_table_output=(fields, {}))
    owner = SimpleNamespace(
        _jobs=queue.Queue(), _results=queue.Queue(), _wake=threading.Event(),
        runtime=None, _operation=job, _worker=object(), _stop=stop,
        _retry_candidate=None, _reload_owner=None,
        _clear_finished_lease=lambda:None, _settle_policy_wait=lambda *a, **k:None,
        _emit_command=lambda *a:None, _emit=lambda *a:None, _restore_policy_commands=lambda *a:None,
        operations=SimpleNamespace(update=lambda identity,status,**extra:receipts.append(status)))
    def run(job):
        if outcome != 'stopped': raise ValueError('effect '+outcome)
        return {}
    owner._run_job = run
    thread = threading.Thread(target=RobotController._manager_loop, args=(owner,), daemon=True)
    thread.start()
    try:
        owner._jobs.put(job)
        assert owner._wake.wait(3)
        RobotController._handle_internal(owner)
        del fields
        gc.collect()
        assert owner._operation is None
        assert receipts[-1] == ('cancelled' if outcome == 'stopped' else outcome)
        assert 'command_table_output' not in job
        assert reference() is None
        assert thread.is_alive()
    finally:
        owner._jobs.put(None)
        thread.join(3)
