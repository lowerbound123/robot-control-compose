"""Named configuration edits target live example instances, including chains."""
import json

import pytest

from config_helpers import read_config
from project_templates import copy_templates
from test_controller import until
from test_example_controller import ROOT, command, example_rc, example_mcp
from test_mcp_management import tool


def activate(rc, mode):
    if mode == 'standalone':
        return 'example'
    directory = copy_templates(ROOT / 'policy/example_chain', rc.registry.directory / 'example_chain')
    manifest = read_config(directory / 'policy.yaml')
    manifest['policies'] = ['example'] * (2 if mode == 'repeated' else 1)
    (directory / 'policy.yaml').write_text(json.dumps(manifest))
    command(rc, 'policy switch example_chain')
    return 'example-1' if mode == 'repeated' else 'example'


@pytest.mark.parametrize('mode', ['standalone', 'unique', 'repeated'])
def test_named_set_preserves_context_and_updates_only_selected_instance(example_rc, mode):
    rc = example_rc
    target = activate(rc, mode)
    group = rc._worker
    workers = group.member_workers() if mode != 'standalone' else {'example': group}
    before = {name: command(rc, f'policy {name} example') for name in workers}
    versions = {name: worker.config_version for name, worker in workers.items()}
    configs = {name: worker.config for name, worker in workers.items()}
    version = group.config_version
    command(rc, 'resume')
    until(lambda: rc.status()['consumed'] >= 3)
    message = '新的 "task"，包含空格'
    command(rc, f'policy {target} config set params.message {json.dumps(message)}')
    assert rc._worker is group and rc.status()['state'] == 'running'
    assert group.config_version == version + 1
    for name, worker in workers.items():
        current = command(rc, f'policy {name} example')
        assert current['context_id'] == before[name]['context_id']
        assert current['module_id'] == before[name]['module_id']
        assert worker.config is configs[name]
        assert current['message'] == (message if name == target else before[name]['message'])
        assert worker.config_version == versions[name] + (name == target)
    command(rc, f'policy {target} config save')
    assert read_config(rc.registry.directory / 'example/policy.yaml')['params']['message'] == message
    command(rc, f'policy {target} config set params.log_every_n_calls 7')
    assert command(rc, f'policy {target} example')['log_every_n_calls'] == 7


def test_named_set_transports_raw_text_and_policy_rejects_invalid_null(example_rc):
    rc = example_rc
    target = activate(rc, 'repeated')
    group = rc._worker
    workers = group.member_workers()
    command(rc, f'policy {target} config set params.message raw text with spaces')
    assert workers[target].config['params']['message'] == 'raw text with spaces'
    version = workers[target].config_version
    command(rc, f'policy {target} config set params.message null', status='failed')
    assert workers[target].config['params']['message'] == 'raw text with spaces'
    assert workers[target].config_version == version
    assert workers['example-0'].config['params']['message'] == 'example'
    assert group is rc._worker and group.member_workers() == workers


@pytest.mark.parametrize('line,detail', [
    ('policy example config set params.message "ambiguous"', 'ambiguous'),
    ('policy missing config set params.message "unknown"', 'not_running'),
    ('policy example-2 config set params.message "unknown"', 'not_running'),
    ('policy example-1 config set params.chunk_size -1', 'chunk_size'),
    ('policy example-1 config set params.unknown 9', 'unsupported configuration key'),
])
def test_invalid_named_target_or_value_keeps_all_instances(example_rc, line, detail):
    rc = example_rc
    activate(rc, 'repeated')
    group = rc._worker
    workers = group.member_workers()
    versions = {name: worker.config_version for name, worker in workers.items()}
    reply = rc.wait_operation(rc.submit(line), 5)
    assert reply['status'] == 'failed' and detail in reply['error'], reply
    assert rc._worker is group and group.member_workers() == workers
    assert {name: worker.config_version for name, worker in workers.items()} == versions
    assert all(worker.config['params']['message'] == 'example' for worker in workers.values())


@pytest.mark.parametrize('interruption', ['pause', 'take', 'policy switch example'])
def test_named_set_cannot_retarget_after_queued_control_change(example_rc, interruption):
    rc = example_rc
    with rc._lock:
        if interruption.startswith('policy switch'):
            changing = (rc.submit_control(interruption) if interruption in ("take", "return") else rc.submit(interruption))
            edit = rc.submit('policy example config set params.message "stale"')
        else:
            edit = rc.submit('policy example config set params.message "stale"')
            changing = (rc.submit_control(interruption) if interruption in ("take", "return") else rc.submit(interruption))
    assert rc.wait_operation(changing, 10)['status'] == 'succeeded'
    assert rc.wait_operation(edit, 10)['status'] == 'cancelled'
    assert rc._worker.config['params']['message'] == 'example'


def test_named_set_reinitializes_standalone_context_without_replacing_worker(example_rc):
    rc = example_rc
    old = rc._worker
    before = command(rc, 'policy example example')
    command(rc, 'policy example config set params.chunk_size 6')
    after = command(rc, 'policy example example')
    assert rc._worker is old and old.alive
    assert after['context_id'] != before['context_id'] and after['module_id'] == before['module_id']
    assert command(rc, 'policy example example')['chunk_size'] == 6
    assert rc.status()['state'] == 'paused'


def test_named_set_through_mcp_requires_take_and_preserves_paused_owner(example_rc, example_mcp):
    rc = example_rc
    target = activate(rc, 'repeated')
    line = f'policy {target} config set params.message "MCP update"'
    assert example_mcp.submit_tool('command', {'line': line}).result(5).is_error
    tool(example_mcp, 'take')
    group = rc._worker
    command(rc, line, status='failed')  # Local writes cannot override MCP ownership.
    tool(example_mcp, 'command', line=line)
    assert rc.control_owner == 'mcp' and rc.status()['state'] == 'paused'
    assert rc._worker is group
    assert group.member_workers()[target].config['params']['message'] == 'MCP update'
    assert group.member_workers()['example-0'].config['params']['message'] == 'example'
