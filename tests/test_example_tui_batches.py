"""Targeted real-worker coverage for the new human batch entry only."""
import pytest

from project_templates import copy_templates
from test_example_controller import ROOT, example_rc, example_mcp


@pytest.mark.parametrize('chain', [False, True])
def test_text_batch_resolves_after_switch_and_updates_exact_example_instance(example_rc, example_mcp, chain):
    rc = example_rc
    if chain:
        copy_templates(ROOT / 'policy/example_chain', rc.registry.directory / 'example_chain')
    target = 'example-1' if chain else 'example'
    commands = ([f'switch example_chain'] if chain else []) + [
        f'set {target}.params.message "from direct text"',
        f'policy {target} set-message "registered command"',
        f'policy {target} example',
    ]
    response = example_mcp.submit_tool('tui_command', {'text': '\n'.join(commands), 'wait': True}).result(15)
    assert not response.is_error, response
    result = response.structured_content
    assert result['status'] == 'succeeded', result
    rows = result['result']['steps']
    assert rows[-1]['result']['message'] == 'registered command'
    assert rc.control_owner == 'policy'
    if chain:
        members = rc._worker.member_workers()
        assert members['example-0'].config['params']['message'] == 'example'
        assert members['example-1'].config['params']['message'] == 'registered command'
