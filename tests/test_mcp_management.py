"""MCP management uses the shipped example and the real SDK host/worker."""
import json
import threading

import pytest

from config_helpers import read_config
from project_templates import copy_templates
from test_controller import until
from test_example_controller import ROOT, command, example_rc, example_mcp
from test_mcp import client


def tool(service, name, *, expected='succeeded', **args):
    response = service.submit_tool(name, {'wait': True, **args}).result(15)
    payload = response.structured_content
    assert payload['status'] == expected, payload
    assert response.is_error == (expected in ('failed', 'cancelled')), payload
    return payload


def query(service, name, **args):
    response = service.submit_tool(name, args).result(5)
    assert not response.is_error, response
    return response.structured_content


@pytest.mark.asyncio
async def test_discovery_and_targeted_help_do_not_import_inactive_policy(example_rc, example_mcp):
    rc = example_rc
    folder = copy_templates(ROOT / 'policy/example', rc.registry.directory / 'inactive')
    config = read_config(folder / 'policy.yaml')
    config['id'] = 'inactive'
    (folder / 'policy.yaml').write_text(json.dumps(config))
    (folder / 'main.py').write_text('raise RuntimeError("discovery imported a model")\n')
    worker = rc._worker
    cursor = rc.read_events()['next_seq']
    async with client(example_mcp.url) as session:
        tools = {item.name for item in (await session.list_tools()).tools}
        assert {'policy_chain', 'record', 'policy_commands', 'policy_command', 'retry_reload'} <= tools
        for name in ('policy_commands', 'help'):
            reply = await session.call_tool(name, {'policy_id': 'inactive'})
            assert not reply.is_error, reply
            row = reply.structured_content['policies'][0]
            assert row['source_id'] == 'inactive' and not row['loaded']
            change = next(c for c in row['commands'] if c['name'] == 'set-chunk-size')
            assert change['args'] == ['int'] and change['effect'] == 'reload_policy'
            assert change['requires_take'] and change['timeout_s'] == 5
        selected = await session.call_tool('help', {'policy_id': 'example', 'command': 'reinitialize'})
        assert [c['name'] for c in selected.structured_content['policies'][0]['commands']] == ['reinitialize']
    assert rc._worker is worker and rc.status()['control_owner'] == 'policy'
    assert rc.read_events(cursor)['events'] == []


def test_text_help_prioritizes_policy_named_record_over_builtin_topic(example_rc, example_mcp):
    folder = copy_templates(ROOT / 'policy/example', example_rc.registry.directory / 'record')
    config = read_config(folder / 'policy.yaml')
    config['id'] = 'record'
    (folder / 'policy.yaml').write_text(json.dumps(config))
    (folder / 'main.py').write_text('raise RuntimeError("help imported policy")\n')
    reply = tool(example_mcp, 'command', line='policy help record')
    assert 'policy record set-chunk-size' in reply['result']['help']
    assert example_rc.control_owner == 'policy'


def test_policy_command_named_return_and_wait_is_not_an_internal_handoff(example_rc, example_mcp):
    rc = example_rc
    path = rc.registry.directory / 'example/policy.yaml'
    config = read_config(path)
    config['tui_commands']['return_and_wait'] = {'handler': 'example', 'args': []}
    path.write_text(json.dumps(config))
    command(rc, 'policy switch example')
    tool(example_mcp, 'take')
    reply = query(example_mcp, 'policy_command', instance_id='example', command='return_and_wait')
    receipt = rc.wait_operation(reply['operation_id'], 2)
    assert receipt['status'] == 'succeeded', receipt
    assert receipt['result']['instance'] == 'example' and rc.get_policy_result()['status'] == 'none'
    with rc._lock:
        pending = rc.submit_policy_command('example', 'return_and_wait')
        rc.submit('pause')
    assert rc.wait_operation(pending, 2)['status'] == 'cancelled'


def test_structured_and_text_commands_share_targets_and_strict_arguments(example_rc, example_mcp):
    rc, service = example_rc, example_mcp
    assert service.submit_tool('policy_command', {'instance_id': 'example', 'command': 'example'}).result(5).is_error
    tool(service, 'take')
    first = tool(service, 'policy_command', instance_id='example', command='example')['result']
    assert tool(service, 'command', line='policy example example')['result'] == first
    for line in ('example', 'example-example'):
        assert service.submit_tool('command', {'line': line, 'wait': True}).result(5).is_error
    version = tool(service, 'policy_command', instance_id='example', command='shared-read')['result']['version']
    for args in (['hello', True], ['hello', str(version)], ['hello'], ['hello', float('inf')]):
        reply = service.submit_tool('policy_command', {
            'instance_id': 'example', 'command': 'shared-write', 'args': args, 'wait': True}).result(5)
        assert reply.is_error
    tool(service, 'policy_command', instance_id='example', command='shared-write', args=['hello "world"', version])
    assert tool(service, 'policy_command', instance_id='example', command='shared-read')['result']['value'] == 'hello "world"'
    assert 'set-chunk-size' in str(tool(service, 'command', line='policy help example')['result'])
    assert rc.status()['control_owner'] == 'mcp'


def test_record_is_independent_of_running_policy_and_wait_episode(example_rc, example_mcp):
    rc, service = example_rc, example_mcp
    command(rc, 'resume')
    until(lambda: rc.status()['consumed'] >= 2)
    epoch = rc.status()['control_epoch']
    generation = rc.buffer.generation
    assert query(service, 'record')['state'] == 'idle'
    started = tool(service, 'record', action='on')['result']
    before = rc.status()['consumed']
    until(lambda: rc.status()['consumed'] > before and rc.status()['recording']['samples_written'] >= 2)
    stopped = tool(service, 'command', line='record stop')['result']
    assert started['session_id'] == stopped['session_id']
    assert stopped['state'] == 'idle' and stopped['pending_bytes'] == 0
    assert rc.status()['control_owner'] == 'policy' and rc.status()['state'] == 'running'
    assert rc.status()['control_epoch'] == epoch and rc.buffer.generation == generation
    tool(service, 'take')
    episode = query(service, 'return_and_wait', timeout_s=0)['operation_id']
    until(lambda: rc.status()['state'] == 'running')
    tool(service, 'command', line='record start')
    tool(service, 'record', action='off')
    assert not query(service, 'get_policy_result', operation_id=episode)['finished']
    command(rc, 'policy example next-result completed recorded')
    assert rc.wait_operation(episode, 5)['status'] == 'succeeded'


def test_chain_apply_and_save_are_separate_and_repeat_instances_are_explicit(example_rc, example_mcp):
    rc, service = example_rc, example_mcp
    assert service.submit_tool('policy_chain', {'policies': ['example']}).result(5).is_error
    tool(service, 'take')
    old = rc._worker
    tool(service, 'policy_chain', policies=['example', 'example'], action='save')
    path = rc.registry.directory / 'default_chain/policy.yaml'
    assert read_config(path)['policies'] == ['example', 'example']
    assert rc._worker is old
    tool(service, 'policy_chain', policies=['example', 'example'])
    group = rc._worker
    members = group.member_workers()
    assert set(members) == {'example-0', 'example-1'} and old.cleanup_confirmed
    assert rc.control_owner == 'mcp' and rc.status()['state'] == 'paused'
    listed = query(service, 'policy_commands', policy_id='example')['policies']
    assert {row['instance_id'] for row in listed} == set(members)
    assert service.submit_tool('policy_command', {'instance_id': 'example', 'command': 'example', 'wait': True}).result(5).is_error
    tool(service, 'policy_chain', policies=['example'], action='save')
    assert rc._worker is group and read_config(path)['policies'] == ['example']
    for invalid in ([], ['missing'], ['default_chain']):
        assert service.submit_tool('policy_chain', {'policies': invalid, 'wait': True}).result(10).is_error
        assert rc._worker is group and group.runnable
    tool(service, 'policy_chain', policies=['example', 'example'])
    assert rc._worker is not group and all(w.cleanup_confirmed for w in members.values())
    assert read_config(path)['policies'] == ['example']


@pytest.mark.parametrize('name', ['set-chunk-size', 'set-chunk-size-all', 'reinitialize-chunk-size'])
def test_mcp_effects_keep_owner_and_obey_instance_scope(example_rc, example_mcp, name):
    rc, service = example_rc, example_mcp
    tool(service, 'take')
    tool(service, 'policy_chain', policies=['example', 'example'])
    before = rc._worker.member_workers()
    contexts = {i: tool(service, 'policy_command', instance_id=i, command='example')['result'] for i in before}
    tool(service, 'policy_command', instance_id='example-1', command=name, args=[6])
    after = rc._worker.member_workers()
    assert rc.control_owner == 'mcp' and rc.status()['state'] == 'paused'
    values = {i: tool(service, 'policy_command', instance_id=i, command='example')['result'] for i in after}
    assert values['example-1']['chunk_size'] == 6 and values['example-0']['chunk_size'] == 4
    if name == 'set-chunk-size-all':
        assert all(before[i] is not after[i] and before[i].cleanup_confirmed for i in before)
    else:
        assert before['example-0'] is after['example-0']
        assert contexts['example-0']['context_id'] == values['example-0']['context_id']
        if name == 'reinitialize-chunk-size':
            assert before['example-1'] is after['example-1']
            assert contexts['example-1']['module_id'] == values['example-1']['module_id']
            assert contexts['example-1']['context_id'] != values['example-1']['context_id']
        else:
            assert before['example-1'].cleanup_confirmed and before['example-1'] is not after['example-1']


def test_mcp_retry_replaces_only_failed_instance(example_rc, example_mcp, monkeypatch):
    rc, service = example_rc, example_mcp
    tool(service, 'take')
    tool(service, 'policy_chain', policies=['example', 'example'])
    before = rc._worker.member_workers()
    def fail(*args, **kwargs):
        raise RuntimeError('injected initialize failure')
    monkeypatch.setattr(before['example-1'], 'initialize_context', fail)
    tool(service, 'policy_command', instance_id='example-1', command='reinitialize', expected='failed')
    assert rc.control_owner == 'mcp' and rc.status()['retry_reload_instance'] == 'example-1'
    assert service.submit_tool('retry_reload', {'instance_id': 'example-0', 'wait': True}).result(5).is_error
    tool(service, 'retry_reload', instance_id='example-1')
    after = rc._worker.member_workers()
    assert after['example-0'] is before['example-0'] and after['example-1'] is not before['example-1']
    assert rc.control_owner == 'mcp' and rc.status()['state'] == 'paused'
    assert rc.status()['retry_reload_instance'] is None


@pytest.mark.parametrize('kind', ['command', 'chain'])
def test_return_cancels_management_requests_queued_in_old_epoch(example_rc, example_mcp, kind):
    rc = example_rc
    tool(example_mcp, 'take')
    old = rc._worker
    with rc._lock:
        identity = (rc.submit_policy_command('example', 'shared-write', ['stale', 0]) if kind == 'command'
                    else rc.submit_policy_chain(['example', 'example']))
        returning = rc.submit_control('return')
    assert rc.wait_operation(returning, 5)['status'] == 'succeeded'
    assert rc.wait_operation(identity, 5)['status'] == 'cancelled'
    assert rc._worker is old and rc.control_owner == 'policy'
    assert command(rc, 'policy example shared-read')['value'] is None


def test_record_can_stop_while_policy_lifecycle_is_busy(example_rc, example_mcp, monkeypatch):
    rc = example_rc
    entered, release = threading.Event(), threading.Event()
    original = rc._run_job
    def delayed(job):
        if job['kind'] == 'switch':
            entered.set()
            assert release.wait(5)
        return original(job)
    monkeypatch.setattr(rc, '_run_job', delayed)
    pending = rc.submit('policy switch example')
    try:
        assert entered.wait(3)
        owner, epoch, generation = rc.control_owner, rc._control_epoch, rc.buffer.generation
        tool(example_mcp, 'record', action='on')
        tool(example_mcp, 'record', action='off')
        assert (rc.control_owner, rc._control_epoch, rc.buffer.generation) == (owner, epoch, generation)
        assert rc._operation is not None
    finally:
        release.set()
    assert rc.wait_operation(pending, 5)['status'] == 'succeeded'


def test_valid_chain_cancels_manual_motion_but_preflight_rejection_does_not(example_rc, example_mcp):
    rc, service = example_rc, example_mcp
    tool(service, 'take')
    reply = query(service, 'send_command', commands='left:up:1@slow')
    assert reply['status'] in ('queued', 'running')
    movement = reply['operation_id']
    until(lambda: rc.direct.active is not None)
    tool(service, 'policy_chain', policies=['missing'], expected='failed')
    assert rc.get_operation(movement)['status'] == 'running'
    tool(service, 'policy_chain', policies=['example', 'example'])
    assert rc.get_operation(movement)['status'] == 'cancelled'
    assert rc.direct.active is None and not rc.direct.pending
    assert rc.control_owner == 'mcp' and rc.status()['state'] == 'paused'


def test_pause_revokes_chain_save_before_atomic_file_replace(example_rc, example_mcp, monkeypatch):
    from robot_control import yaml_config
    rc, service = example_rc, example_mcp
    tool(service, 'take')
    tool(service, 'policy_chain', policies=['example'], action='save')
    path = rc.registry.directory / 'default_chain/policy.yaml'
    before = path.read_bytes()
    entered, release = threading.Event(), threading.Event()
    original = yaml_config.render_yaml_config
    def delayed(target, values, **kwargs):
        output = original(target, values, **kwargs)
        if target == path:
            entered.set()
            assert release.wait(5)
        return output
    monkeypatch.setattr(yaml_config, 'render_yaml_config', delayed)
    pending = service.submit_tool('policy_chain', {'policies': ['example', 'example'], 'action': 'save', 'wait': True})
    try:
        assert entered.wait(3)
        command(rc, 'pause')
    finally:
        release.set()
    assert pending.result(5).is_error
    assert path.read_bytes() == before and rc.control_owner == 'mcp'


def test_chain_commit_can_precede_control_thread_retirement_notification(example_rc, example_mcp, monkeypatch):
    rc = example_rc
    tool(example_mcp, 'take')
    original = rc._handle_internal
    def delayed_notifications():
        job = rc._operation
        if job and job['kind'] == 'switch' and job.get('prepared') and not job.get('committed'):
            with rc._results.mutex:
                completed = any(row['type'] == 'job_done' for row in rc._results.queue)
            if not completed:
                return
        original()
    monkeypatch.setattr(rc, '_handle_internal', delayed_notifications)
    tool(example_mcp, 'policy_chain', policies=['example', 'example'])
    assert rc._worker.runnable and rc.status()['policy'] == 'default_chain'
    assert set(rc._worker.member_workers()) == {'example-0', 'example-1'}
