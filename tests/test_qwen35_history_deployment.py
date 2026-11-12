import hashlib
import json
import shutil

from history_helpers import ROOT


def test_independent_policy_is_discovered_and_container_binds_its_own_sources(tmp_path):
    from robot_control.policy_catalog import discover
    from robot_control.yaml_config import load_yaml_config
    source = ROOT / 'policy/qwen35_history'
    assert (source / 'main.py').exists(), 'production entry missing'
    destination = tmp_path / 'policy/qwen35_history'
    shutil.copytree(source, destination)
    shutil.copyfile(destination / 'policy.yaml.example', destination / 'policy.yaml')
    rows = discover(destination.parent)
    assert rows[0]['valid'] and rows[0]['id'] == 'qwen35_history'
    model = load_yaml_config(source / 'compose.yaml')
    service = model['services']['policy']
    assert service['image'] != 'robot-control-compose-qwen35:local'
    modules = json.loads(service['environment']['RC_SOURCE_MODULES'])
    assert modules['lerobot'] == '/workspace/policy/qwen35_history/vendor'
    assert modules['rc_qwen35_history'] == '/workspace/policy/qwen35_history/src'
    assert service['restart'] == 'no'
    for mount in service['volumes']:
        if '/workspace/' in mount['target'] or '/site-packages/' in mount['target']:
            assert mount['read_only'] is True
    assert service['environment']['USER'] == service['environment']['LOGNAME'] == 'robot-control'


def test_vendor_hashes_and_dependency_locks_are_complete():
    vendor = ROOT / 'policy/qwen35_history/vendor'
    manifest = json.loads((vendor / 'SOURCE_MANIFEST.json').read_text())
    assert manifest['source_commit'].startswith('314c3fc')
    for record in manifest['files']:
        assert hashlib.sha256((vendor / record['file']).read_bytes()).hexdigest() == record['sha256']
    for record in manifest['derived_files']:
        assert hashlib.sha256((ROOT / record['file']).read_bytes()).hexdigest() == record['sha256']
    assert (vendor / 'LICENSE').is_file() and (vendor / 'LICENSE-APACHE-2.0').is_file()
    lock = ROOT / 'policy/qwen35_history/dependencies/requirements.lock'
    assert lock.is_file(), 'locked image dependencies missing'
    # Parse pinned distributions, not formatting or order of generated hash lines.
    pins = dict(line.strip().rstrip('\\').strip().split('==',1) for line in lock.read_text().splitlines()
                if '==' in line and not line.lstrip().startswith('#'))
    assert pins['torch'] == '2.10.0+cu128' and pins['transformers'] == '5.2.0'
    assert pins['torchvision'] == '0.25.0+cu128'


def test_scope_includes_new_tests_and_not_other_qwen_models():
    import importlib.util
    spec = importlib.util.spec_from_file_location('history_test_scope', ROOT / 'scripts/test_scope.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert 'qwen35_history' in module.POLICIES
    selected = module.suite({'qwen35_history'}, False)
    assert 'tests/test_qwen35_history_runtime.py' in selected
    assert 'tests/test_example_worker.py' in selected
    assert 'tests/test_qwen35_backend.py' not in selected
    old_scope = module.suite({'qwen35_single','qwen35_twostage'}, False)
    assert not any('test_qwen35_history_' in name for name in old_scope)
