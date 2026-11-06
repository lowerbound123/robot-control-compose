import json

import pytest

from robot_control.history import CommandHistory, valid_history_line


@pytest.mark.parametrize('nested', [False, True])
def test_history_rejects_sensitive_text_batches_before_persistence(tmp_path, nested):
    line = 'mcp tui_command ' + json.dumps({'text': 'status\npolicy example config set params.api_key "SECRET-CANARY"'})
    if nested:
        line = 'mcp command ' + json.dumps({'line': line})
    store = CommandHistory(tmp_path / 'rc.yaml', tmp_path / 'outputs')
    try:
        assert store.wait_idle(3)
        store.record('status')
        with pytest.raises(ValueError, match='密钥'):
            store.record(line)
        assert store.wait_idle(3)
        assert 'SECRET-CANARY' not in store.path.read_text()
        # A pre-existing history file must also be screened before ghost display.
        store.close()
        store.path.write_text(json.dumps({'version': 1, 'commands': [line]}))
        loaded = CommandHistory(tmp_path / 'rc.yaml', tmp_path / 'outputs')
        try:
            assert loaded.wait_idle(3)
            assert loaded.snapshot()[0] == ()
        finally:
            loaded.close()
    finally:
        store.close()


def test_history_batch_guard_preserves_safe_raw_text_and_rejects_malformed_secret_wrapper():
    line = 'mcp tui_command ' + json.dumps({'text': 'status\npolicy example config set params.message 中文'}, ensure_ascii=False)
    assert valid_history_line(line) == line
    with pytest.raises(ValueError, match='密钥'):
        valid_history_line('mcp tui_command {"text":"config rc set mcp.api_key SECRET-CANARY')
