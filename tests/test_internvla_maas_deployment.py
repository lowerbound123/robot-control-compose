import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from config_helpers import read_config
from internvla_maas_helpers import ROOT, PACKAGE
from project_templates import copy_templates
from robot_control.policy_catalog import discover
from test_setup_dynamic import read_compose


def test_discovery_setup_and_reload_command_do_not_require_model_dependencies(tmp_path):
    source = ROOT / 'policy/internvla_maas'
    assert source.is_dir(), 'network policy entry missing'
    directory = tmp_path / 'policy/internvla_maas'
    copy_templates(source, directory)
    records = discover(tmp_path / 'policy')
    assert records[0]['valid'] is True
    config = read_config(directory / 'policy.yaml')
    assert config['tui_commands']['switch-model']['effect'] == 'reload_policy'
    from rc_policy_sdk.configuration import apply_patch
    from rc_internvla_maas.configuration import validate_manifest
    assert config['execution']['mode'] == 'async'
    for mode in ('sync', 'async'):
        apply_patch(config, {'execution.mode': mode, 'execution.call_every_actions': 4})
        validate_manifest(config)
    sys.path.insert(0, str(ROOT / 'scripts'))
    from config_files import configuration_updates, write_updates
    from setup_project import select_policies
    assert select_policies(tmp_path, ['internvla_maas']) == ['internvla_maas']
    private = directory / 'model.json'
    private.unlink()
    shutil.copyfile(source / 'model.json.example', directory / 'model.json.example')
    changes, _ = configuration_updates(tmp_path)
    write_updates(tmp_path, changes)
    assert private.stat().st_mode & 0o777 == 0o600
    assert read_config(private)['service']['api_key'] == ''
    result = subprocess.run([sys.executable, '-c',
        'import sys; import rc_internvla_maas.configuration; assert not ({"torch", "transformers", "rclpy", "websockets"} & sys.modules.keys())'],
        env={**os.environ, 'PYTHONPATH': str(PACKAGE)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_compose_needs_no_gpu_weights_or_extra_base_services():
    path = ROOT / 'policy/internvla_maas/compose.yaml'
    assert path.is_file(), 'network policy deployment missing'
    model = read_compose(path)
    assert set(model['services']) == {'policy'}
    service = model['services']['policy']
    assert service['image'] == 'robot-control-compose-internvla-maas:local'
    assert service.get('deploy', {}).get('resources', {}).get('reservations', {}).get('devices') is None
    assert {v['target'] for v in service['volumes']} == {'/workspace/policy/internvla_maas/src'}
    assert json.loads(service['environment']['RC_SOURCE_MODULES']) == {
        'rc_internvla_maas': '/workspace/policy/internvla_maas/src'}


def test_selected_scope_includes_new_policy_and_example_only():
    path = ROOT / 'scripts/test_scope.py'
    spec = importlib.util.spec_from_file_location('maas_test_scope', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert 'internvla_maas' in module.POLICIES
    selected = module.suite({'internvla_maas'}, False)
    assert 'tests/test_internvla_maas_backend.py' in selected
    assert any('test_example_' in p for p in selected)
    assert not any('test_qwen35_' in p or 'test_pi05_' in p for p in selected)
