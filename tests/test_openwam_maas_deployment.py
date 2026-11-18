"""Discovery, source selection, and consumer-only Compose configuration."""
import json
import os
import shutil
import subprocess

import pytest

from test_openwam_maas import ROOT, SOURCES


@pytest.mark.parametrize('source', SOURCES)
def test_source_is_discovered_and_keeps_its_own_network(tmp_path, monkeypatch, source):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    from config_files import configuration_updates, write_updates
    from config_validation import validate_documents
    from setup_project import select_policies
    from test_scope import POLICIES, suite

    folder = tmp_path / 'policy' / source
    folder.mkdir(parents=True)
    for name in ('policy.yaml.example', 'compose.yaml', 'main.py'):
        shutil.copyfile(ROOT / 'policy' / source / name, folder / name)
    changes, docs = configuration_updates(tmp_path)
    validate_documents(docs)
    write_updates(tmp_path, changes)
    assert select_policies(tmp_path, [source]) == [source]
    assert source in POLICIES and 'tests/test_openwam_maas.py' in suite({source}, False)
    if source == 'openwam_maas_lift2':
        path = folder / 'policy.yaml'
        from robot_control.yaml_config import render_yaml_config
        saved = docs[f'policy/{source}/policy.yaml']
        saved['params'].update(gripper_lower_threshold=None, gripper_upper_value='+0.2')
        path.write_bytes(render_yaml_config(path, saved))
        _, updated = configuration_updates(tmp_path)
        assert updated[f'policy/{source}/policy.yaml']['params'] == saved['params']

    if not shutil.which('docker'):
        pytest.skip('Docker Compose CLI required')
    model = json.loads(subprocess.check_output(
        ['docker', 'compose', '--env-file', '/dev/null', '-f', str(folder / 'compose.yaml'), 'config', '--format', 'json'],
        env=dict(os.environ, OPENWAM_NETWORK=source + '_test')))
    assert set(model['services']) == {'policy'}
    assert model['networks']['openwam']['name'] == source + '_test'
    assert model['networks']['openwam']['external'] is True
    service = model['services']['policy']
    assert service['image'] == 'robot-control-compose-openwam-maas:local'
    assert service['environment']['PYTHONPATH'] == f'/workspace/policy/{source}/src'
