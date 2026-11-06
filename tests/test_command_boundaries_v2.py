import pytest

from robot_control.secret_config import reject_secret_edit
from robot_control.terminal_batches import parse_batch


@pytest.mark.parametrize('line', [
    'config rc set mcp.api_key "secret"',
    'policy example config set params.api_key "secret"',
    'global set task {"api_key":"secret"}',
    'mcp policy_config {"instance_id":"example","action":"set","key":"params.api_key","value_text":"secret"}',
])
def test_sensitive_new_command_paths_are_rejected_before_history(line):
    with pytest.raises(ValueError, match='密钥'):
        reject_secret_edit(line)


@pytest.mark.parametrize('line', ['policy manage', 'policy manage save', 'monitor example', 'camera show', 'snapshot', 'mcp take'])
def test_batch_uses_command_model_for_local_only_boundaries(line):
    with pytest.raises(ValueError):
        parse_batch('status\n' + line)


def test_batch_preserves_policy_owned_raw_apostrophe_text():
    line = "policy example config set params.message don't move"
    assert parse_batch(line) == [(1, line)]
