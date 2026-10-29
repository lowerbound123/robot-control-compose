from config_helpers import read_config
import copy
import json
from pathlib import Path
import sys

import pytest

from test_controller import setup_controller as setup_controller, ready

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'packages/llm-direction-policy/src'))


def test_provider_uses_config_key_even_when_parent_environment_conflicts(monkeypatch):
    pytest.importorskip('openai', reason='provider client requires the optional LLM environment')
    from rc_llm_direction import providers
    from rc_llm_direction.configuration import DEFAULT_PARAMS
    calls = []
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'wrong-parent-key')
    monkeypatch.setenv('OPENAI_API_KEY', 'wrong-global-key')
    monkeypatch.setattr(providers, 'OpenAI', lambda **kwargs: calls.append(kwargs))
    settings = {**DEFAULT_PARAMS['provider'], 'api_key': 'own-policy-key'}
    settings.pop('api_key_env', None)
    providers.ProviderClient(settings)
    assert calls[0]['api_key'] == 'own-policy-key'
    settings['api_key'] = ''
    with pytest.raises(ValueError, match='api_key'):
        providers.ProviderClient(settings)
    assert len(calls) == 1


def test_direct_key_schema_and_obsolete_selector():
    from rc_llm_direction.configuration import DEFAULT_PARAMS, validate_config
    config = {'execution': {'control_mode': 'epos'}, 'hot_reloadable': [],
              'params': {**copy.deepcopy(DEFAULT_PARAMS), 'task': 'test'}}
    config['params']['provider'].pop('api_key_env', None)
    config['params']['provider']['api_key'] = 'own-policy-key'
    assert validate_config(config)['provider']['api_key'] == 'own-policy-key'
    config['params']['provider']['api_key_env'] = 'DEEPSEEK_API_KEY'
    with pytest.raises(ValueError, match='api_key'):
        validate_config(config)


def test_public_configuration_and_pending_status_hide_keys_but_save_preserves_them(setup_controller):
    from test_controller import ready
    controller, _, _ = setup_controller
    ready(controller)
    record = controller.registry.get(controller._identity)
    record.config['params']['provider'] = {'api_key': 'private-saved-key'}
    controller._pending_config = {'path': 'params.provider.api_key', 'value': 'private-pending-key', 'hot': False}
    public = controller.query('show policy')
    assert public['params']['provider']['api_key'] == '***'
    assert 'private-pending-key' not in json.dumps(controller.status())
    controller.submit('show policy')
    path = controller.registry.save(controller._identity)
    assert read_config(path)['params']['provider']['api_key'] == 'private-saved-key'
    assert record.config['params']['provider']['api_key'] == 'private-saved-key'


@pytest.mark.parametrize('line', [
    'set policy.params.provider.api_key "hidden-key"',
    'set policy.params.provider {"api_key":"hidden-key"}',
    'mcp command set policy.params.provider.api_key "hidden-key"',
    'mcp command ' + json.dumps({'line': 'set policy.params.provider.api_key "hidden-key"'}),
])
def test_secret_edits_are_rejected_before_history_or_receipts(line, setup_controller):
    from robot_control.terminal_commands import TerminalCommandRouter
    from robot_control.tui import CommandInput
    controller, _, _ = setup_controller
    ready(controller)
    router = TerminalCommandRouter(controller)
    try:
        with pytest.raises(ValueError, match='配置文件'):
            router.submit(line)
        command = CommandInput()
        with pytest.raises(ValueError, match='配置文件'):
            command.remember(line)
        assert command.history == []
        assert not controller.operations._items
    finally:
        router.close()


def test_controller_direct_submission_rejects_key_before_receipt(setup_controller):
    controller, _, _ = setup_controller
    ready(controller)
    with pytest.raises(ValueError, match='配置文件'):
        controller.submit('set policy.params.provider.api_key "hidden-key"', origin='mcp')
    assert not controller.operations._items


def test_worker_json_logs_and_operation_results_hide_keys(setup_controller):
    controller, _, _ = setup_controller
    ready(controller)
    controller._emit('[worker] ' + json.dumps({'provider': {'api_key': 'log-secret', 'model': 'test'}}))
    assert 'log-secret' not in json.dumps(controller.read_events())
    identity = controller.operations.create('test result')
    controller.operations.update(identity, 'succeeded', result={'api_key':'receipt-secret'})
    assert controller.operations.get(identity)['result']['api_key'] == '***'


@pytest.mark.asyncio
async def test_tui_secret_submission_is_not_echoed_or_remembered(setup_controller):
    from robot_control.tui import RobotControlApp, CommandInput
    from robot_control.log_tui import StructuredLog
    controller, _, _ = setup_controller
    ready(controller)
    app = RobotControlApp(controller, start_controller=False)
    async with app.run_test(size=(100, 30)) as pilot:
        command = app.query_one(CommandInput)
        command.value = 'set policy.params.provider.api_key "typed-secret"'
        await pilot.press('enter')
        assert command.history == []
        assert 'typed-secret' not in str(app.query_one(StructuredLog).records)
        assert app.is_running


def test_operation_results_hide_structured_keys():
    from robot_control.operations import Operations
    operations = Operations()
    identity = operations.create('read result')
    operations.update(identity, 'succeeded', result={'api_key':'private-receipt'})
    assert operations.get(identity)['result']['api_key'] == '***'


@pytest.mark.parametrize('style', ['repr', 'escaped', 'fully_escaped'])
def test_worker_log_syntaxes_hide_keys_in_events_and_persistent_log(style, setup_controller):
    import io
    controller, _, _ = setup_controller
    ready(controller)
    value = {'provider': {'api_key': 'review-private-canary'}}
    text = repr(value) if style == 'repr' else json.dumps(value)
    if style == 'escaped':
        text = text.replace('api_key', chr(92) + 'u0061pi_key')
    elif style == 'fully_escaped':
        text = text.replace('api_key', ''.join(chr(92) + 'u%04x' % ord(c) for c in 'api_key'))
    stream = io.StringIO()
    previous = controller._log_stream
    controller._log_stream = stream
    try:
        controller._emit('[worker] ' + text)
        assert 'review-private-canary' not in stream.getvalue()
        assert 'review-private-canary' not in json.dumps(controller.read_events())
    finally:
        controller._log_stream = previous
