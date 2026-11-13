import copy
import json
from types import SimpleNamespace

import pytest

from rc_policy_sdk.configuration import apply_candidate
from history_helpers import model_file
from test_qwen35_history_gripper_config import entry
from test_qwen35_history_runtime import Backend


@pytest.fixture
def loaded(entry, tmp_path, monkeypatch):
    module, active, source = entry
    path = model_file(tmp_path, robot_type='arx_lift2' if source.endswith('lift2') else 'arx_acone')
    active['params']['model_config_path'] = 'model.json'
    monkeypatch.setattr(module, 'POLICY_ROOT', tmp_path)
    implementation = module.HistoryPolicy
    backend = Backend()
    monkeypatch.setattr(module, 'HistoryPolicy', lambda config, **kwargs: implementation(config, backend=backend, **kwargs))
    context = module.initialize(active, runtime=SimpleNamespace())
    try:
        yield module, active, context, backend, path
    finally:
        context.close()


@pytest.mark.parametrize('key,value,effect', [
    ('execution.replan_every_actions', 45, 'reset'),
    ('execution.pipelined', True, 'reset'),
    ('execution.inference_timeout_s', 17, None),
    ('runtime.instruction', 'new task', 'reset'),
    ('history.enabled', False, 'reset'),
    ('inference.generate_language', ['Subtask'], 'reinitialize'),
    ('inference.num_inference_steps', 7, 'reinitialize'),
    ('inference.use_cuda_graph', False, 'reinitialize'),
    ('transition.max_smoothing_steps', 5, 'reset'),
])
def test_model_settings_use_memory_snapshot_and_select_lifecycle(loaded, key, value, effect):
    module, active, context, backend, path = loaded
    path.write_text('external file edit must not be read')
    candidate, effects = copy.deepcopy(active), []
    module.configure(context, candidate, 'model_config.' + key, json.dumps(value), effects.append)
    assert effects == ([] if effect is None else [effect])
    assert active != candidate
    exported = module.export_config(context, candidate)
    section, leaf = key.split('.')
    assert exported['settings']['model_config'][section][leaf] == value
    assert exported['files'][0]['data']['model']['checkpoint'] == 'weights'
    assert exported['files'][0]['path'] == 'model.json'
    assert not any(k.startswith('_') for k in exported['policy']['params'])
    if effect != 'reinitialize':
        apply_candidate(active, candidate)
        if effect == 'reset': module.reset_context(context)
        assert sum(event[0] == 'load' for event in backend.events) == 1
        if key == 'execution.replan_every_actions': assert context.x == value
        if key == 'execution.pipelined': assert context.pipelined is value


@pytest.mark.parametrize('key,value', [('execution.replan_every_actions', 0),
    ('execution.replan_every_actions', True), ('execution.pipelined', 'true'),
    ('transition.max_smoothing_steps', 0), ('transition.trunk_mode', 'unknown'),
    ('inference.generate_language', ['Other']), ('execution.unknown', 1)])
def test_invalid_model_setting_has_no_effect(loaded, key, value):
    module, active, context, _, _ = loaded
    candidate, effects = copy.deepcopy(active), []
    with pytest.raises(ValueError):
        module.configure(context, candidate, 'model_config.' + key, json.dumps(value), effects.append)
    assert candidate == active and effects == []


def test_switch_model_preserves_relative_source_path_and_discards_unsaved_snapshot(loaded):
    module, active, context, _, _ = loaded
    candidate = copy.deepcopy(active)
    module.configure(context, candidate, 'model_config.execution.replan_every_actions', '45', lambda _: None)
    apply_candidate(active, candidate)
    patch = module.switch_model(context, 'model.json')
    assert patch['params.model_config_path'] == 'model.json'
    assert patch['params._model_config_snapshot'] is None
