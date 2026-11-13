"""Policy-owned parsing and lifecycle selection, without loading model backends."""
import copy
import importlib.util
from pathlib import Path

import pytest

from config_helpers import read_config

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ('example', 'hold', 'llm_direction', 'llm_failure', 'pi05',
           'qwen35_single', 'qwen35_twostage', 'qwen35_history', 'qwen35_history_lift2', 'internvla_maas')


@pytest.fixture
def entry(monkeypatch):
    for directory in [*(ROOT/'packages').glob('*/src'), *(ROOT/'policy').glob('*/src')]:
        monkeypatch.syspath_prepend(str(directory))

    def load(source):
        path = ROOT / 'policy' / source
        spec = importlib.util.spec_from_file_location('configure_' + source, path / 'main.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = read_config(path / 'policy.yaml.example')
        if source == 'llm_direction':
            config['params']['task'] = 'move the cup'
        return module, config
    return load


@pytest.mark.parametrize('source', SOURCES)
def test_every_source_owns_its_configure_handler(entry, source):
    module, _ = entry(source)
    assert callable(getattr(module, 'configure', None)), source


@pytest.mark.parametrize('source,key,raw,expected,effect', [
    ('example', 'params.message', '"带有 \\"引号\\" 的消息"', '带有 "引号" 的消息', None),
    ('example', 'params.message', 'plain text with spaces', 'plain text with spaces', None),
    ('example', 'params.message', "'single quoted text'", 'single quoted text', None),
    ('example', 'params.log_every_n_calls', '7', 7, None),
    ('example', 'params.inference_delay_s', '0.25', .25, None),
    ('example', 'params.reset_token', '1', 1, 'reset'),
    ('example', 'params.chunk_size', '3', 3, 'reinitialize'),
    ('example', 'params.reader_url', '"http://reader:8080"', 'http://reader:8080', 'reinitialize'),
    ('hold', 'params.chunk_size', '12', 12, None),
    ('llm_direction', 'params.task', 'move another cup', 'move another cup', 'reinitialize'),
    ('llm_direction', 'params.translation_m', '0.02', .02, 'reinitialize'),
    ('llm_failure', 'params.history_size', '8', 8, 'reinitialize'),
    ('qwen35_single', 'params.task', '"move cup"', 'move cup', 'reset'),
    ('qwen35_twostage', 'params.task', 'null', None, 'reset'),
    ('qwen35_history', 'params.task', 'move cup', 'move cup', 'reset'),
    ('example', 'execution.mode', 'async', 'async', None),
    ('hold', 'execution.call_every_actions', '3', 3, None),
    ('internvla_maas', 'execution.mode', 'sync', 'sync', None),
    ('llm_failure', 'execution.control_mode', 'mixed', 'mixed', 'reinitialize'),
    ('example', 'execution.control_mode', 'epos', 'epos', 'reinitialize'),
])
def test_configuration_edits_only_candidate_and_selects_effect(entry, source, key, raw, expected, effect):
    module, active = entry(source)
    before = copy.deepcopy(active)
    candidate = copy.deepcopy(active)
    context = {'config': active, 'sentinel': object()}
    snapshot = dict(context)
    effects = []
    assert module.configure(context, candidate, key, raw, effects.append) is None
    section, leaf = key.split('.')
    assert candidate[section][leaf] == expected
    assert effects == ([] if effect is None else [effect])
    assert active == before
    assert context == snapshot


@pytest.mark.parametrize('source', SOURCES)
def test_command_worker_configuration_requires_reinitialize(entry, source):
    module, config = entry(source)
    effects = []
    module.configure(object(), config, 'execution.tui_command_workers', '3', effects.append)
    assert config['execution']['tui_command_workers'] == 3
    assert effects == ['reinitialize']


@pytest.mark.parametrize('source,key,raw', [
    *[(source, 'id', 'other') for source in SOURCES],
    *[(source, 'params.unknown', '1') for source in SOURCES],
    *[(source, 'execution.tui_command_workers', 'true') for source in SOURCES],
    ('example', 'params.chunk_size', '0'),
    ('example', 'params.chunk_size', 'true'),
    ('example', 'params.message', '{"nested": [1, 2]}'),
    ('example', 'params.message', '"unterminated'),
    ('example', 'params.reset_token', '-1'),
    ('example', 'params.inference_delay_s', 'NaN'),
    ('hold', 'params.chunk_size', '-1'),
    ('hold', 'execution.control_mode', 'epos'),
    ('llm_direction', 'params.task', '""'),
    ('llm_direction', 'params.chunk_size', '0'),
    ('llm_direction', 'params.request_when_remaining', '30'),
    ('llm_direction', 'execution.control_mode', 'qpos'),
    ('llm_failure', 'params.history_size', '1'),
    ('llm_failure', 'params.task', 'unsupported'),
    ('qwen35_history', 'execution.control_mode', 'epos'),
    ('qwen35_history', 'params.task', '"  "'),
    ('internvla_maas', 'execution.control_mode', 'epos'),
])
def test_rejected_configuration_changes_nothing(entry, source, key, raw):
    module, candidate = entry(source)
    before = copy.deepcopy(candidate)
    effects = []
    with pytest.raises(ValueError):
        module.configure(object(), candidate, key, raw, effects.append)
    assert candidate == before
    assert effects == []


@pytest.mark.parametrize('source', ('pi05', 'qwen35_single', 'qwen35_twostage',
                                     'qwen35_history', 'internvla_maas'))
def test_model_file_is_validated_without_calling_live_context(entry, source, monkeypatch):
    module, candidate = entry(source)
    calls = []
    monkeypatch.setattr(module, 'load_model_config', lambda path, **kwargs: calls.append((path, kwargs)))
    effects = []
    module.configure(object(), candidate, 'params.model_config_path', '"new model.json"', effects.append)
    assert candidate['params']['model_config_path'] == 'new model.json'
    assert effects == ['reinitialize']
    assert len(calls) == 1 and calls[0][0] == 'new model.json'
    assert calls[0][1]['project_root'] == module.POLICY_ROOT
    if source in ('qwen35_single', 'qwen35_twostage'):
        assert calls[0][1]['workflow'] == ('single' if source == 'qwen35_single' else 'two_stage')


@pytest.mark.parametrize('source', ('pi05', 'qwen35_single', 'qwen35_twostage',
                                     'qwen35_history', 'internvla_maas', 'llm_direction', 'llm_failure'))
def test_missing_model_or_provider_is_rejected_before_candidate_changes(entry, source, tmp_path):
    module, candidate = entry(source)
    before = copy.deepcopy(candidate)
    effects = []
    leaf = 'provider_config_path' if source.startswith('llm_') else 'model_config_path'
    with pytest.raises((ValueError, FileNotFoundError)):
        module.configure(object(), candidate, 'params.' + leaf, str(tmp_path / 'missing'), effects.append)
    assert candidate == before
    assert effects == []


def test_example_reset_token_has_a_meaningful_context_reset(entry):
    module, active = entry('example')
    context = module.initialize(active)
    context['stats']['calls'] = 4
    context['pending_result'] = ('completed', 'stale task')
    candidate = copy.deepcopy(active)
    effects = []
    module.configure(context, candidate, 'params.reset_token', '1', effects.append)
    assert context['stats']['calls'] == 4
    assert context['pending_result'] == ('completed', 'stale task')
    assert effects == ['reset']
    module.reset_context(context)
    assert context['stats']['calls'] == 0
    assert context['pending_result'] is None


@pytest.mark.parametrize('source', SOURCES)
def test_connector_switch_action_is_policy_owned_and_applies_to_future_switch(entry, source):
    module, config = entry(source)
    for value in ('reset', 'reinitialize_policy'):
        effects = []
        module.configure(object(), config, 'execution.on_connector_switch', value, effects.append)
        assert config['execution']['on_connector_switch'] == value
        assert effects == []
    before = copy.deepcopy(config)
    with pytest.raises(ValueError):
        module.configure(object(), config, 'execution.on_connector_switch', 'unknown', effects.append)
    assert config == before and effects == []


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('mode', ['sync', 'async'])
def test_rc_owns_mode_and_call_cadence_for_every_policy(entry, source, mode):
    from rc_policy_sdk.catalog import validate_policy
    from rc_policy_sdk.configuration import validate_candidate
    module, active = entry(source)
    candidate, effects = copy.deepcopy(active), []
    module.configure(object(), candidate, 'execution.mode', mode, effects.append)
    module.configure(object(), candidate, 'execution.call_every_actions', '7', effects.append)
    validate_candidate(active, candidate, None)
    validate_policy(candidate)
    validator = getattr(module, 'validate_config', None)
    if validator is not None: validator(candidate)
    assert candidate['execution']['mode'] == mode
    assert candidate['execution']['call_every_actions'] == 7
    assert not effects


def test_shared_sdk_still_rejects_unknown_execution_mode(entry):
    from rc_policy_sdk.configuration import validate_candidate
    module, active = entry('example')
    candidate = copy.deepcopy(active)
    module.configure(object(), candidate, 'execution.mode', 'unknown', lambda _: None)
    with pytest.raises(ValueError, match='mode'):
        validate_candidate(active, candidate, None)
