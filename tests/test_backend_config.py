"""Connection profiles and the common policy switch contract."""
import copy
import json
from pathlib import Path

import pytest

from config_helpers import read_config
from robot_control.config import _validate_rc, validate_policy
from test_config_buffer import manifest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('value', ['reload_policy', '', None, True, 1])
def test_policy_rejects_unknown_backend_switch_action(value):
    config = manifest()
    config['execution']['on_backend_switch'] = value
    with pytest.raises(ValueError, match='on_backend_switch'):
        validate_policy(config)


def test_profile_selector_accepts_named_connection_but_rejects_inline_mix():
    config = read_config(ROOT / 'configs/rc.mock.yaml.example')
    config['robot'] = {'profile': 'lab-left'}
    config['robot_profiles_directory'] = 'robots'
    _validate_rc(config)
    config['robot']['backend'] = 'mock'
    with pytest.raises(ValueError):
        _validate_rc(config)


def test_profile_reads_fresh_file_without_accepting_paths_or_symlinks(tmp_path):
    from robot_control.backend_profiles import BackendProfiles
    folder = tmp_path / 'robots'
    folder.mkdir()
    path = folder / 'lab.yaml'
    path.write_text(json.dumps({'connector': 'body', 'source': 'mock', 'config': 'devices/first.yaml'}))
    profiles = BackendProfiles(folder)
    first = profiles.read('lab')
    path.write_text(json.dumps({'connector': 'body', 'source': 'mock', 'config': 'devices/second.yaml'}))
    assert profiles.read('lab')['config'] == 'devices/second.yaml'
    assert first['config'] == 'devices/first.yaml'
    for name in ('../lab', '/lab', 'lab.yaml'):
        with pytest.raises(ValueError):
            profiles.read(name)
    (folder / 'alias.yaml').symlink_to(path)
    with pytest.raises(ValueError):
        profiles.read('alias')


def test_setup_upgrades_policy_field_without_overwriting_hot_list_or_choice(tmp_path):
    from test_setup_config import helpers
    folder = tmp_path / 'policy/demo'
    folder.mkdir(parents=True)
    config = manifest()
    config['id'] = 'demo'
    config['execution']['on_backend_switch'] = 'reset'
    (folder / 'policy.yaml').write_text(json.dumps(config))
    template = copy.deepcopy(config)
    template['execution']['on_backend_switch'] = 'reinitialize_policy'
    template['hot_reloadable'] = ['execution.on_backend_switch']
    (folder / 'policy.yaml.example').write_text(json.dumps(template))
    changes, documents = helpers().configuration_updates(tmp_path)
    updated = documents['policy/demo/policy.yaml']
    assert updated['execution']['on_backend_switch'] == 'reset'
    assert updated['hot_reloadable'] == ['params.nested.value', 'execution.on_backend_switch']


def test_setup_keeps_direct_connector_and_creates_profiles_only_once(tmp_path):
    from test_setup_config import helpers
    direct = {'robot': {'connector': 'body'}, 'connector_directory': 'connector',
              'connectors': {'body': {'source': 'mock', 'config': 'devices/custom.yaml'}}}
    (tmp_path / 'rc.yaml').write_text(json.dumps(direct))
    (tmp_path / 'rc.yaml.example').write_text(json.dumps({'robot': {'profile': 'mock'}, 'robot_profiles_directory': 'configs/robots'}))
    folder = tmp_path / 'configs/robots'
    folder.mkdir(parents=True)
    profile = {'connector': 'body', 'source': 'mock', 'config': 'devices/default.yaml'}
    (folder / 'mock.yaml.example').write_text(json.dumps(profile))
    changes, documents = helpers().configuration_updates(tmp_path)
    assert documents['rc.yaml']['robot'] == direct['robot']
    assert documents['rc.yaml']['connectors'] == direct['connectors']
    assert documents['configs/robots/mock.yaml'] == profile
    helpers().write_updates(tmp_path, changes)
    profile['config'] = 'devices/custom.yaml'
    (folder / 'mock.yaml').write_text(json.dumps(profile))
    assert helpers().configuration_updates(tmp_path)[1]['configs/robots/mock.yaml'] == profile


def test_setup_checks_custom_profiles_without_a_template(tmp_path):
    from test_setup_config import helpers
    import importlib.util
    spec = importlib.util.spec_from_file_location('backend_setup_validation', ROOT / 'scripts/config_validation.py')
    validation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validation)
    folder = tmp_path / 'configs/robots'
    folder.mkdir(parents=True)
    (folder / 'custom.yaml').write_text('backend: mock\n')
    _, documents = helpers().configuration_updates(tmp_path)
    assert 'configs/robots/custom.yaml' in documents
    with pytest.raises(ValueError, match='configs/robots/custom.yaml'):
        validation.validate_documents(documents)
