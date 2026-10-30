import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_rc_explicit_json_and_sibling_conflicts_are_rejected(tmp_path):
    from robot_control.config import load_rc
    old = tmp_path/'rc.json'
    old.write_text('{"schema_version":3}')
    with pytest.raises(ValueError, match='YAML|yaml|migration|迁移'):
        load_rc(old)
    path = tmp_path/'rc.yaml'
    path.write_text('schema_version: 3\n')
    with pytest.raises(ValueError, match='migration|迁移|conflict'):
        load_rc(path)


def test_catalog_manual_migration_and_default_chain_cannot_overwrite_legacy(tmp_path):
    from robot_control.policy_catalog import discover, read_descriptor
    from robot_control.config import PolicyRegistry
    folder = tmp_path/'default_chain'
    folder.mkdir()
    old = folder/'policy.json'
    original = '{"schema_version":3,"kind":"chain","policies":["a"]}'
    old.write_text(original)
    assert discover(tmp_path)[0]['state'] == 'invalid'
    with pytest.raises(ValueError, match='migration|迁移'):
        read_descriptor(folder)
    with pytest.raises(ValueError, match='migration|迁移'):
        PolicyRegistry(tmp_path).save_chain(['a'])
    assert old.read_text() == original and not (folder/'policy.yaml').exists()


def test_setup_rejects_main_json_but_keeps_business_json_and_compose(tmp_path):
    sys.path.insert(0,str(ROOT/'scripts'))
    from config_files import configuration_updates, write_updates
    folder=tmp_path/'policy/custom'
    folder.mkdir(parents=True)
    (folder/'policy.yaml.example').write_text('params: {} # schema checked separately\n')
    (folder/'policy.json').write_text('{}')
    with pytest.raises(ValueError, match='migration|迁移'):
        configuration_updates(tmp_path)
    assert not (folder/'policy.yaml').exists()
    (folder/'policy.json').unlink()
    (folder/'model.json.example').write_text('{"model":"original"}')
    (folder/'compose.yaml').write_text('services: !invalid untouched')
    checkpoint=folder/'weights'
    checkpoint.mkdir()
    (checkpoint/'policy.json').write_text('{"external":"model-format"}')
    changes, documents=configuration_updates(tmp_path)
    write_updates(tmp_path,changes)
    assert 'policy/custom/policy.yaml' in documents
    assert json.loads((folder/'model.json').read_text()) == {'model':'original'}
    assert (folder/'compose.yaml').read_text() == 'services: !invalid untouched'


def test_setup_existing_yml_is_updated_in_place_and_comments_survive(tmp_path):
    sys.path.insert(0,str(ROOT/'scripts'))
    from config_files import configuration_updates, write_updates
    (tmp_path/'rc.yaml.example').write_text('a: 1\nb: false # default b\n')
    path=tmp_path/'rc.yml'
    path.write_text('# private\na: 3 # local\n')
    changes, documents=configuration_updates(tmp_path)
    assert 'rc.yml' in documents
    write_updates(tmp_path,changes)
    assert '# private' in path.read_text() and '# local' in path.read_text() and '# default b' in path.read_text()
    assert not (tmp_path/'rc.yaml').exists()
    assert not configuration_updates(tmp_path)[0]
    (tmp_path/'rc.yaml').write_text('a: 4\n')
    with pytest.raises(ValueError, match='conflict'):
        configuration_updates(tmp_path)


def test_host_and_down_imports_do_not_require_yaml_or_array_dependencies():
    code = '''
import sys,importlib.abc
from pathlib import Path
root=Path(sys.argv[1])
sys.path[:0]=[str(root/'scripts'),str(root/'src'),str(root/'packages/policy-sdk/src')]
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in ('ruamel','yaml','numpy'):
            raise AssertionError('unexpected dependency: '+fullname)
sys.meta_path.insert(0,Block())
import compose_project
from robot_control.runtime_manager import ComposeRuntimeManager
from robot_control.external import ExternalResources
from rc_policy_sdk.host import PolicyHost
'''
    subprocess.run([sys.executable,'-I','-c',code,str(ROOT)],check=True)


def test_schema_errors_from_files_do_not_echo_values(tmp_path):
    from robot_control.config import load_rc
    from robot_control.policy_catalog import read_descriptor
    path=tmp_path/'rc.yaml'
    path.write_text('schema_version: 3\nfrequency_hz: 30\nrobot: {backend: mock}\nreset_qpos: [private-secret]\n')
    with pytest.raises(ValueError) as error: load_rc(path)
    assert 'private-secret' not in str(error.value)
    folder=tmp_path/'example';folder.mkdir()
    (folder/'policy.yaml').write_text('schema_version: 3\nexecution: {mode: sync, call_every_actions: 1, control_mode: qpos}\nparams: {}\nhot_reloadable: [private-secret]\n')
    with pytest.raises(ValueError) as error: read_descriptor(folder)
    assert 'private-secret' not in str(error.value)


def test_setup_schema_errors_do_not_echo_values():
    sys.path.insert(0, str(ROOT/'scripts'))
    from config_validation import validate_documents
    value = {'schema_version':3,'execution':{'mode':'sync','call_every_actions':1,'control_mode':'qpos'},
             'params':{},'hot_reloadable':['private-secret']}
    with pytest.raises(ValueError) as error:
        validate_documents({'policy/example/policy.yaml': value})
    assert 'private-secret' not in str(error.value)
    assert 'policy/example/policy.yaml' in str(error.value)
