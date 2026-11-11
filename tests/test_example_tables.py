"""Shipped example tables through direct callbacks, real workers and a self-chain."""
import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from config_helpers import read_config
from host_helpers import LocalPolicyProcess
from project_templates import copy_templates
from rc_policy_sdk import TableOutput
from rc_policy_sdk.commands import command_specs
from rc_policy_sdk.tables import normalize_table_output, table_document_text
from test_controller import until
from test_example_controller import ROOT, command, example_rc, example_mcp
from test_example_policy import example, config, images
from test_example_worker import command_result


def table_events(rc, *, after=0, command_id=None, event=None):
    return [row for row in rc.read_events(after_seq=after, limit=1000)['events']
            if 'table_document' in row
            and (command_id is None or row.get('command_id') == command_id)
            and (event is None or row.get('event') == event)]


def assert_table_event(event):
    assert event['message'] == table_document_text(event['table_document'])
    assert event['message'] and json.loads(event['copy_json'])
    assert event['source_id'] == 'example'
    assert all(event.get(key) for key in ('instance_id', 'runtime_id', 'session_id', 'context_id'))


def test_template_registers_explicit_table_command_and_default_off_logs(config):
    spec = command_specs(config)['table-stats']
    assert spec['handler'] == 'table_stats' and spec['args'] == []
    assert config['params']['table_logs'] is False
    assert config['config_hints']['settings']['params.table_logs']['type'] == 'bool'


@pytest.mark.parametrize('missing', [False, True])
def test_default_callbacks_keep_plain_output_without_table_calls(example, config, images, capsys, missing):
    if missing:
        config['params'].pop('table_logs')
    config['params']['log_every_n_calls'] = 2
    runtime = SimpleNamespace(observation_id='sample', consumed_actions=0, control_epoch=1,
                              config_version=1, log_table=lambda *_: pytest.fail('default table log'))
    context = example.initialize(config, runtime=runtime)
    assert capsys.readouterr().out == 'example initialize：本地示例已就绪\n'
    for _ in range(2):
        example.policy(context, {}, [[0] * 14], images, config, runtime=runtime)
        example.observe(context, {}, images, config, runtime=runtime)
    assert capsys.readouterr().out == 'example policy #2：example\n'
    assert isinstance(example.example(context), dict)


@pytest.mark.parametrize('invalid', [0, 1, None, 'true', [], {}])
def test_table_logs_requires_actual_bool_and_rejected_config_is_unchanged(example, config, invalid):
    candidate = copy.deepcopy(config)
    candidate['params']['table_logs'] = invalid
    with pytest.raises(ValueError, match='table_logs'):
        example.validate_config(candidate)
    context = example.initialize(config)
    before = copy.deepcopy(config)
    with pytest.raises(ValueError, match='table_logs'):
        example.configure(context, config, 'params.table_logs', json.dumps(invalid),
                          lambda *_: pytest.fail('invalid setting requested lifecycle work'))
    assert config == before and context['config'] is config


def test_table_logs_configure_is_an_ordinary_update(example, config):
    context = example.initialize(config)
    reference, params = context['config'], context['config']['params']
    for value in ('true', 'false'):
        example.configure(context, config, 'params.table_logs', value,
                          lambda *_: pytest.fail('table_logs requested lifecycle work'))
        assert config is reference and config['params'] is params
        assert config['params']['table_logs'] is json.loads(value)


def test_table_stats_preserves_full_snapshot_and_typed_detached_cells(example, config, images):
    runtime = SimpleNamespace(observation_id='sample', consumed_actions=0, control_epoch=1, config_version=1)
    context = example.initialize(config)
    example.observe(context, {}, images, config, runtime=runtime)
    plain = example.example(context)
    output = example.table_stats(context)
    assert isinstance(output, TableOutput)
    result, document = normalize_table_output(output)
    assert result == plain
    summary, activity, cameras = document['tables']
    assert summary['columns'] == ['实例', 'Python', '推理次数', '观测次数', '已释放']
    assert summary['rows'] == [['example', plain['python'], 0, 1, False]]
    assert activity['rows'][0][-1] == 'example'
    assert cameras['columns'] == ['相机', '尺寸', '类型', '时间戳', '只读', '采样']
    assert cameras['rows'][0] == ['head', [2, 3, 3], 'uint8', 12.5, True, '7']
    output.result['observations'] = 999
    output.tables[0].rows[0][0] = 'detached'
    assert example.table_stats(context).result == plain
    assert example.monitor(context) == plain


def test_active_logs_sample_policy_and_observe_independently(example, config, images, capsys):
    config['params'].update(table_logs=True, log_every_n_calls=2)
    outputs = []
    def log(output):
        outputs.append(output)
        return False  # A full best-effort queue must not change policy behavior.
    runtime = SimpleNamespace(observation_id='sample', consumed_actions=0, control_epoch=1,
                              config_version=1, log_table=log)
    context = example.initialize(config, runtime=runtime)
    actions = [[0] * 14]
    assert example.policy(context, {}, actions, images, config, runtime=runtime) is actions
    for _ in range(2):
        example.observe(context, {}, images, config, runtime=runtime)
    assert [output.tables[0].title for output in outputs] == ['example initialize', 'example observe']
    assert outputs[-1].result['observations'] == 2 and outputs[-1].result['calls'] == 1
    assert example.policy(context, {}, actions, images, config, runtime=runtime) is actions
    assert [output.tables[0].title for output in outputs] == [
        'example initialize', 'example observe', 'example policy']
    assert outputs[-1].result['calls'] == 2 and outputs[-1].result['observations'] == 2
    assert capsys.readouterr().out == 'example initialize：本地示例已就绪\nexample policy #2：example\n'
    config['params']['table_logs'] = False
    for _ in range(2):
        example.policy(context, {}, actions, images, config, runtime=runtime)
        example.observe(context, {}, images, config, runtime=runtime)
    assert len(outputs) == 3


def test_real_example_initialization_table_before_ready_and_old_runtime_rejected(tmp_path):
    folder = copy_templates(ROOT / 'policy/example', tmp_path / 'example')
    config = read_config(folder / 'policy.yaml')
    config['params']['table_logs'] = True
    gate = tmp_path / 'finish-initialize'
    # A test-only wrapper holds the real example initialize open after its log,
    # and probes the saved real runtime during same-module reinitialization.
    with (folder / 'main.py').open('a') as stream:
        stream.write(f'''
from pathlib import Path
_example_initialize = initialize
_old_table_context = None
_old_table_error = None
def initialize(config, *, runtime=None):
    global _old_table_context, _old_table_error
    if _old_table_context is not None:
        try:
            _old_table_context['session'].log_table(table_stats(_old_table_context))
        except RuntimeError as error:
            _old_table_error = str(error)
        else:
            raise AssertionError('old example runtime accepted a table')
    context = _example_initialize(config, runtime=runtime)
    _old_table_context = context
    deadline = time.monotonic() + 10
    while not Path({str(gate)!r}).exists():
        if time.monotonic() > deadline:
            raise TimeoutError('test initialization gate')
        time.sleep(.01)
    return context
def old_table_error(context):
    return _old_table_error
''')
    config['tui_commands']['old-table-error'] = {'handler': 'old_table_error', 'args': []}
    events = []
    worker = LocalPolicyProcess(folder, config, on_log_event=events.append,
                               init_timeout_s=15, inference_timeout_s=5, release_timeout_s=2)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            started = pool.submit(worker.start)
            try:
                until(lambda: events, 10)
                assert not worker._ready
                assert events[0]['table_document']['tables'][0]['title'] == 'example initialize'
            finally:
                gate.touch()
            started.result(10)
        assert worker.supports_table_output and worker.host.table_output_v1
        worker.submit_command('table', 'table-stats', [])
        reply = command_result(worker)
        assert reply['result']['calls'] == reply['result']['observations'] == 0
        assert reply['result']['instance'] == 'example'
        assert reply['table_document']['schema_version'] == 1
        old_context, old_pid = worker.context_id, worker.pid
        worker.release_context()
        candidate = copy.deepcopy(worker.config)
        flags = worker.initialize_context(candidate, 2)
        until(lambda: len(events) == 2)
        assert events[-1]['context_id'] == worker.context_id != old_context
        assert events[-1]['config_version'] == 2 and worker.config_version == 1
        assert worker.pid == old_pid
        worker.commit_reinitialize(candidate, 2, flags)
        worker.resume_commands()
        worker.submit_command('stale', 'old-table-error', [])
        assert 'no longer active' in command_result(worker)['result']
        assert len(events) == 2
        for event in events:
            assert_table_event(event)
    finally:
        gate.touch()
        assert not worker.close()


def test_real_example_self_chain_tables_logs_config_and_effects(example_rc):
    rc = example_rc
    assert not table_events(rc, event='policy.log')
    folder = rc.registry.directory / 'example'
    manifest = read_config(folder / 'policy.yaml')
    manifest['params'].update(table_logs=True, log_every_n_calls=2)
    manifest['tui_commands']['table-size'] = {
        'handler': 'table_size', 'args': ['int'], 'effect': 'reinitialize_policy'}
    (folder / 'policy.yaml').write_text(json.dumps(manifest))
    with (folder / 'main.py').open('a') as stream:
        stream.write('''
def table_size(context, size):
    return TableOutput(result={'params.chunk_size': size},
                       tables=[Table(title='candidate size', columns=['size'], rows=[[size]])])
''')
    copy_templates(ROOT / 'policy/example_chain', rc.registry.directory / 'example_chain')
    cursor = rc.read_events(limit=1000)['next_seq']
    command(rc, 'policy switch example_chain')
    group = rc._worker
    workers = group.member_workers()
    assert set(workers) == {'example-0', 'example-1'}
    until(lambda: len(table_events(rc, after=cursor, event='policy.log')) == 2)
    initialized = table_events(rc, after=cursor, event='policy.log')
    assert {row['instance_id'] for row in initialized} == set(workers)
    assert len({row['runtime_id'] for row in initialized}) == 2
    assert len({json.loads(row['copy_json'])['context_id'] for row in initialized}) == 2
    for instance, worker in workers.items():
        assert worker.supports_table_output and worker.host.table_output_v1
        ordinary = command(rc, f'policy {instance} example')
        identity = rc.submit(f'policy {instance} table-stats')
        receipt = rc.wait_operation(identity, 8)
        assert receipt['status'] == 'succeeded' and receipt['result'] == ordinary
        until(lambda: table_events(rc, command_id=identity))
        event, = table_events(rc, command_id=identity)
        assert_table_event(event)
        assert json.loads(event['copy_json']) == receipt['result']
        assert event['runtime_id'] == worker.runtime_id and event['context_id'] == worker.context_id
        assert event['instance_id'] == instance
        receipt['result']['instance'] = 'edited caller copy'
        assert command(rc, f'policy {instance} example') == ordinary

    before = {name: command(rc, f'policy {name} example') for name in workers}
    version = workers['example-1'].config_version
    success = rc.submit('policy example-1 table-size 5')
    receipt = rc.wait_operation(success, 10)
    assert receipt['status'] == 'succeeded' and receipt['result']['state'] == 'paused'
    until(lambda: table_events(rc, command_id=success))
    effect_event, = table_events(rc, command_id=success)
    assert json.loads(effect_event['copy_json']) == {'params.chunk_size': 5}
    after = {name: command(rc, f'policy {name} example') for name in workers}
    assert after['example-0'] == before['example-0']
    assert after['example-1']['context_id'] != before['example-1']['context_id']
    assert after['example-1']['module_id'] == before['example-1']['module_id']
    assert workers['example-1'].config_version == version + 1
    failed = rc.submit('policy example-1 table-size -1')
    rejection = rc.wait_operation(failed, 10)
    assert rejection['status'] == 'failed' and 'chunk_size' in rejection['error']
    assert not table_events(rc, command_id=failed)
    assert command(rc, 'policy example-1 example') == after['example-1']
    assert group is rc._worker and group.member_workers() == workers

    cursor = rc.read_events(limit=1000)['next_seq']
    command(rc, 'resume')
    expected = {(instance, f'{instance} {phase}') for instance in workers for phase in ('policy', 'observe')}
    def active_kinds():
        return {(row['instance_id'], row['table_document']['tables'][0]['title'])
                for row in table_events(rc, after=cursor, event='policy.log')}
    until(lambda: expected <= active_kinds(), 8)
    command(rc, 'pause')
    until(lambda: rc._inflight is None and rc._observe_inflight is None)
    for event in table_events(rc, after=cursor, event='policy.log'):
        assert_table_event(event)
        snapshot = json.loads(event['copy_json'])
        phase = event['table_document']['tables'][0]['title'].rsplit(' ', 1)[-1]
        assert snapshot['calls' if phase == 'policy' else 'observations'] % 2 == 0
        assert snapshot['instance'] == event['instance_id']

    for instance, worker in workers.items():
        context = command(rc, f'policy {instance} example')
        reference, params, version = worker.config, worker.config['params'], worker.config_version
        command(rc, f'policy {instance} config set params.table_logs false')
        assert worker.config is reference and worker.config['params'] is params
        assert worker.config_version == version + 1
        assert command(rc, f'policy {instance} example') == context
    cursor = rc.read_events(limit=1000)['next_seq']
    consumed = rc.status()['consumed']
    command(rc, 'resume')
    until(lambda: rc.status()['consumed'] >= consumed + 12, 8)
    command(rc, 'pause')
    until(lambda: rc._inflight is None and rc._observe_inflight is None)
    assert not table_events(rc, after=cursor, event='policy.log')


@pytest.mark.asyncio
async def test_table_stats_http_mcp_keeps_json_result_and_event_copy(example_rc, example_mcp):
    from test_mcp import client
    rc = example_rc
    async with client(example_mcp.url) as session:
        assert not (await session.call_tool('take', {'wait': True})).is_error
        reply = await session.call_tool('policy_command', {
            'instance_id': 'example', 'command': 'table-stats', 'wait': True})
        assert not reply.is_error
        receipt = reply.structured_content
        assert json.loads(reply.content[0].text) == receipt
        assert receipt['status'] == 'succeeded'
        assert receipt['result'] == command(rc, 'policy example example')
        assert 'tables' not in receipt['result'] and 'table_document' not in receipt['result']
        until(lambda: table_events(rc, command_id=receipt['operation_id']))
        event, = table_events(rc, command_id=receipt['operation_id'])
        assert json.loads(event['copy_json']) == receipt['result']
        assert rc.control_owner == 'mcp' and rc.status()['state'] == 'paused'


def test_docker_table_group_with_real_local_example_chain(example_rc, example_mcp):
    """Validate the new smoke group itself without launching any Docker container."""
    from example_docker_smoke import table_output_checks
    from test_mcp import client
    rc = example_rc
    copy_templates(ROOT / 'policy/example_chain', rc.registry.directory / 'example_chain')
    command(rc, 'policy switch example_chain')
    report = {'example_python': command(rc, 'policy example-0 example')['python'], 'checks': []}

    def call(name, **arguments):
        async def request():
            async with client(example_mcp.url) as session:
                return await session.call_tool(name, arguments)
        reply = asyncio.run(request())
        assert not reply.is_error, reply
        assert json.loads(reply.content[0].text) == reply.structured_content
        return reply.structured_content

    def identities():
        return {name: dict(host=worker.host.instance_id, process=worker.runtime_id,
                           pid=worker.pid, version=worker.config_version)
                for name, worker in rc._worker.member_workers().items()}

    def callbacks_drained():
        with rc._lock:
            return rc._inflight is None and rc._observe_inflight is None

    call('take', wait=True)
    table_output_checks(rc, report, call, identities, callbacks_drained)
    assert len(report['checks']) == 1
    assert set(report['tables']['commands']) == {'example-0', 'example-1'}
    assert all(not worker.config['params']['table_logs'] and worker.config['params']['log_every_n_calls'] == 100
               for worker in rc._worker.member_workers().values())
