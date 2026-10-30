from config_helpers import read_config
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


def plugin(root, name='fifth-policy', **extra):
    folder = root / name
    folder.mkdir(parents=True)
    config = dict(schema_version=3, execution=dict(mode='sync', call_every_actions=1, control_mode='qpos'), params={}, **extra)
    (folder / 'policy.yaml').write_text(json.dumps(config))
    (folder / 'main.py').write_text('raise RuntimeError("discovery must not import")\ntype Modern = int\n')
    (folder / 'compose.yaml').write_text('services:\n  policy:\n    image: fixture:local\n')
    return folder


def test_catalog_discovers_formal_manifest_without_template_or_import(tmp_path):
    from robot_control.policy_catalog import discover, read_descriptor
    folder = plugin(tmp_path)
    rows = discover(tmp_path)
    assert rows[0]['id'] == 'fifth-policy'
    assert rows[0]['state'] == 'discovered'
    descriptor = read_descriptor(folder)
    assert descriptor['config']['id'] == folder.name
    assert descriptor['socket'] == '/run/robot-control/fifth-policy.sock'
    from config_files import configuration_updates
    _, documents = configuration_updates(tmp_path.parent)
    # Bootstrap's real-file discovery has the same contract (policy root).
    root = tmp_path / 'project'
    target = plugin(root / 'policy')
    _, documents = configuration_updates(root)
    assert 'policy/fifth-policy/policy.yaml' in documents


@pytest.mark.parametrize('name,extra', [('Upper', {}), ('x'*49, {}), ('okay', {'id':'other'}), ('okay', {'policy_host':'old'})])
def test_identity_is_directory_and_legacy_fields_are_rejected(tmp_path, name, extra):
    from robot_control.policy_catalog import read_descriptor
    with pytest.raises(ValueError):
        read_descriptor(plugin(tmp_path, name, **extra))


def test_refresh_preserves_active_snapshot_and_reads_disk(tmp_path):
    from robot_control.config import PolicyRegistry
    folder = plugin(tmp_path, params_override=True)
    registry = PolicyRegistry(tmp_path)
    old = registry.get(folder.name)
    value = read_config(folder / 'policy.yaml')
    value['params'] = {'new': 5}
    (folder / 'policy.yaml').write_text(json.dumps(value))
    current = registry.get(folder.name, refresh=True)
    assert old.config['params'] == {}
    assert current.config['params'] == {'new': 5}
    assert current.version > old.version


def test_templates_do_not_make_a_policy_runnable(tmp_path):
    from robot_control.policy_catalog import discover
    folder = plugin(tmp_path)
    (folder / 'policy.yaml').rename(folder / 'policy.yaml.example')
    assert discover(tmp_path)[0]['state'] == 'needs_config'


def test_symlink_alias_cannot_change_directory_identity(tmp_path):
    from robot_control.policy_catalog import read_descriptor
    folder=plugin(tmp_path,'real')
    (tmp_path/'alias').symlink_to(folder,target_is_directory=True)
    with pytest.raises(ValueError,match='symlink'):
        read_descriptor(tmp_path/'alias')
