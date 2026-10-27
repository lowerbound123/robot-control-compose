from project_templates import template_path, copy_templates
import importlib.util
import json
from pathlib import Path

import pytest

from test_qwen35_configuration import model_config as model_config
from test_qwen35_runtime import Model, images, metadata

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('identity,workflow', [('qwen35_single', 'single'), ('qwen35_twostage', 'two_stage')])
def test_independent_entries_bind_workflow_and_use_registered_reload(identity, workflow, model_config, monkeypatch):
    directory = ROOT / 'policy' / identity
    assert (directory / 'main.py').exists(), 'independent policy entry is missing'
    spec = importlib.util.spec_from_file_location(identity, directory / 'main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = json.loads(template_path(directory / 'policy.json').read_text())
    from robot_control.config import validate_policy
    from rc_policy_sdk.commands import command_specs, resolve_handlers
    validate_policy(config)
    commands = command_specs(config)
    assert commands['switch-model']['effect'] == 'reload_policy'
    handler = resolve_handlers(module, commands)['switch-model']
    own_model = json.loads(template_path(directory / 'model.json').read_text())
    assert own_model['model']['workflow'] == workflow
    assert own_model['model']['checkpoint']
    path, value = model_config
    value['model']['workflow'] = workflow
    path.write_text(json.dumps(value))
    config['params']['model_config_path'] = str(path)
    from rc_qwen35_policy.runtime import AsyncQwenPolicy
    backend = Model()
    monkeypatch.setattr(module, 'AsyncQwenPolicy', lambda value, **kwargs: AsyncQwenPolicy(value, backend=backend, **kwargs))
    context = module.initialize(config, runtime=metadata(0))
    try:
        chunk = module.policy(context, {'qpos': [0] * 14}, [], images(), config, runtime=metadata(0))
        assert chunk.shape == (50, 14)
        assert handler(context, str(path)) == {'params.model_config_path': str(path)}
        value['model']['workflow'] = 'single' if workflow == 'two_stage' else 'two_stage'
        path.write_text(json.dumps(value))
        with pytest.raises(ValueError, match='workflow'): handler(context, str(path))
        module.reset_context(context)
    finally:
        module.release(context)
    assert [name for name, _ in backend.events].count('close') == 1


def test_entry_discovery_does_not_import_model_framework(tmp_path):
    from robot_control.config import PolicyRegistry
    import subprocess
    import sys
    registry = PolicyRegistry(copy_templates(ROOT / 'policy', tmp_path / 'policy'))
    for identity in ('qwen35_single', 'qwen35_twostage'):
        assert registry.get(identity).config['id'] == identity
        assert 'conda_env' not in registry.get(identity).config
    script = 'import sys; import rc_qwen35_policy; assert not ({"torch", "transformers", "rclpy"} & sys.modules.keys())'
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True,
                            env={'PYTHONPATH': str(ROOT / 'packages/qwen35-policy/src')})
    assert result.returncode == 0, result.stderr
