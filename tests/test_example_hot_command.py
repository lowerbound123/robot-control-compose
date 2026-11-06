"""Registered hot-patch commands use the same config transaction as set."""
import json
import threading

import pytest

from config_helpers import read_config
from project_templates import copy_templates
from test_controller import until
from test_example_controller import ROOT, command, example_rc, example_mcp
from test_mcp_management import tool, query


@pytest.mark.parametrize('chain', [False, True])
def test_hot_command_preserves_context_and_commits_only_target(example_rc, chain):
    rc = example_rc
    if chain:
        copy_templates(ROOT / 'policy/example_chain', rc.registry.directory / 'example_chain')
        command(rc, 'policy switch example_chain')
    group = rc._worker
    workers = group.member_workers() if chain else {'example': group}
    target = 'example-1' if chain else 'example'
    before = {name: command(rc, f'policy {name} example') for name in workers}
    versions = {name: worker.config_version for name, worker in workers.items()}
    version = group.config_version
    command(rc, 'resume')
    until(lambda: rc.status()['consumed'] >= 3)
    command(rc, f'policy {target} set-message "new message"')
    assert rc._worker is group and rc.status()['state'] == 'running'
    assert group.config_version == version + 1
    for name, worker in workers.items():
        current = command(rc, f'policy {name} example')
        assert current['context_id'] == before[name]['context_id']
        assert current['module_id'] == before[name]['module_id']
        assert current['message'] == ('new message' if name == target else before[name]['message'])
        assert worker.config_version == versions[name] + (name == target)
    command(rc, f'policy {target} config save')
    assert read_config(rc.registry.directory / 'example/policy.yaml')['params']['message'] == 'new message'
    command(rc, f'policy {target} set-message "second message"')  # Command gate is reopened.
    assert command(rc, f'policy {target} example')['message'] == 'second message'


def test_hot_command_rejects_a_cold_leaf_without_changing_context(example_rc):
    rc = example_rc
    path = rc.registry.directory / 'example/policy.yaml'
    manifest = read_config(path)
    manifest['hot_reloadable'].remove('params.message')
    path.write_text(json.dumps(manifest))
    command(rc, 'policy switch example')
    assert 'policy example set-message' in rc.local_commands()
    worker, version = rc._worker, rc._worker.config_version
    before = command(rc, 'policy example example')
    command(rc, 'policy example set-message rejected', status='failed')
    assert rc._worker is worker and worker.config_version == version
    assert command(rc, 'policy example example')['context_id'] == before['context_id']
    assert command(rc, 'policy example example')['message'] == before['message']


def test_hot_command_requires_mcp_take_and_keeps_control(example_rc, example_mcp):
    rc = example_rc
    assert example_mcp.submit_tool('policy_command', {
        'instance_id': 'example', 'command': 'set-message', 'args': ['forbidden']}).result(5).is_error
    spec = query(example_mcp, 'help', policy_id='example', command='set-message')['policies'][0]['commands'][0]
    assert spec['effect'] == 'update_config' and spec['requires_take']
    tool(example_mcp, 'take')
    worker = rc._worker
    tool(example_mcp, 'policy_command', instance_id='example', command='set-message', args=['MCP task'])
    assert rc._worker is worker and worker.config['params']['message'] == 'MCP task'
    assert rc.control_owner == 'mcp' and rc.status()['state'] == 'paused'


def test_hot_command_result_after_pause_cannot_commit(example_rc, monkeypatch):
    rc = example_rc
    worker, version = rc._worker, rc._worker.config_version
    poll = worker.poll_command
    pending, received, release = [], threading.Event(), threading.Event()
    def delayed_result():
        if pending:
            return pending.pop() if release.is_set() else None
        message = poll()
        if message and message['type'] == 'command_result':
            pending.append(message)
            received.set()
            return None
        return message
    monkeypatch.setattr(worker, 'poll_command', delayed_result)
    identity = rc.submit('policy example set-message stale')
    try:
        assert received.wait(3)
        command(rc, 'pause')
    finally:
        release.set()
    assert rc.wait_operation(identity, 5)['status'] == 'cancelled'
    assert worker.config_version == version and worker.config['params']['message'] == 'example'


@pytest.mark.parametrize('line', ['policy example set-message stale', 'policy example config set params.message "stale"'])
def test_hot_command_ack_after_pause_cannot_commit_registry(example_rc, monkeypatch, line):
    rc = example_rc
    worker, version = rc._worker, rc._worker.config_version
    method = 'commit_config' if ' config set ' in line else 'update_config'
    update = getattr(worker, method)
    applied, release = threading.Event(), threading.Event()
    def delayed_ack(*args, **kwargs):
        update(*args, **kwargs)
        applied.set()
        assert release.wait(3)
    monkeypatch.setattr(worker, method, delayed_ack)
    identity = rc.submit(line)
    try:
        assert applied.wait(3)
        command(rc, 'pause')
    finally:
        release.set()
    assert rc.wait_operation(identity, 5)['status'] == 'cancelled'
    if method == 'commit_config':
        assert rc._worker is worker and not worker.runnable
        assert rc.status()['retry_reload_instance'] == 'example'
    else:
        until(lambda: rc._worker is None)
        assert worker.cleanup_confirmed
    record = rc.registry.get('example')
    assert record.version == version and record.config['params']['message'] == 'example'


@pytest.mark.parametrize('ack_version', [None, 1, '2'])
def test_hot_command_rejects_mismatched_ack_version(example_rc, monkeypatch, ack_version):
    rc = example_rc
    worker, version = rc._worker, rc._worker.config_version
    receive = worker._receive
    def wrong_ack(packet):
        event = packet['event']
        if event['type'] == 'ack' and 'config_version' in event:
            packet = {**packet, 'event': {**event, 'config_version': ack_version}}
        receive(packet)
    monkeypatch.setattr(worker, '_receive', wrong_ack)
    command(rc, 'policy example set-message unconfirmed', status='failed')
    until(lambda: rc._worker is None)
    assert worker.cleanup_confirmed and worker.config_version == version
    record = rc.registry.get('example')
    assert record.version == version and record.config['params']['message'] == 'example'
