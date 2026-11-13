import copy
import json
from contextlib import contextmanager

import pytest

from test_configure_protocol import POLICY, worker


EXPORT = '''
def export_config(context, config):
    return {"policy": config, "settings": {"model_config": {"amount": config["params"]["amount"]}},
            "files": [{"path": "model.json", "data": {"amount": config["params"]["amount"]}}]}
'''


def test_export_follows_ready_and_confirmed_configuration(tmp_path):
    with worker(tmp_path, POLICY + EXPORT) as (send, recv, ready, config):
        assert ready['config_export']['settings']['model_config']['amount'] == 1
        send('configure_prepare', key='amount', value='7')
        prepared = recv()
        send('configure_apply', config=prepared['config'], action=None, new_config_version=2)
        assert recv()['type'] == 'ack'
        send('configure_commit', new_config_version=2)
        result = recv()
        assert result['config_export']['files'] == [{'path': 'model.json', 'data': {'amount': 7}}]
        assert ready['config_export']['settings']['model_config']['amount'] == 1


def snapshot():
    return {'policy': {'id': 'example', 'params': {'value': 2}},
            'settings': {'model_config': {'value': 2}},
            'files': [{'path': 'model.json', 'data': {'value': 2}}]}


def test_save_export_preserves_snapshot_and_rolls_back_failed_second_replace(tmp_path, monkeypatch):
    from robot_control.config_export import save_export
    import robot_control.config_export as impl
    manifest = tmp_path / 'policy.yaml'
    model = tmp_path / 'model.json'
    manifest.write_text('id: example\nparams: {value: 1}\n')
    model.write_text('{"value": 1}\n')
    before = {p: p.read_bytes() for p in (manifest, model)}
    exported = snapshot()
    preserved = copy.deepcopy(exported)
    original, calls = impl.os.replace, []
    def replace(source, target):
        calls.append(target)
        if len(calls) == 2: raise OSError('injected disk failure')
        return original(source, target)
    with monkeypatch.context() as patch:
        patch.setattr(impl.os, 'replace', replace)
        with pytest.raises(OSError, match='injected'):
            save_export(tmp_path, manifest, exported)
    assert {p: p.read_bytes() for p in before} == before
    assert exported == preserved
    save_export(tmp_path, manifest, exported)
    assert json.loads(model.read_text()) == {'value': 2}
    assert exported == preserved
    assert sorted(p.name for p in tmp_path.iterdir()) == ['model.json', 'policy.yaml']


@pytest.mark.parametrize('path', ['../model.json', '/tmp/model.json', 'main.py', 'model.json.example'])
def test_invalid_export_destination_never_writes(tmp_path, path):
    from robot_control.config_export import save_export
    exported = snapshot()
    exported['files'][0]['path'] = path
    with pytest.raises(ValueError): save_export(tmp_path, tmp_path / 'policy.yaml', exported)
    assert list(tmp_path.iterdir()) == []


def test_save_rechecks_guard_before_replacing_any_file(tmp_path):
    from robot_control.config_export import save_export
    @contextmanager
    def stale():
        raise RuntimeError('configuration changed')
        yield
    with pytest.raises(RuntimeError, match='changed'):
        save_export(tmp_path, tmp_path / 'policy.yaml', snapshot(), guard=stale)
    assert list(tmp_path.iterdir()) == []


def test_export_cannot_replace_manifest_as_json_or_shadow_identity(tmp_path):
    from robot_control.config_export import save_export
    from rc_policy_sdk.config_export import validate_export
    exported = snapshot()
    exported['files'][0]['path'] = 'policy.json'
    with pytest.raises(ValueError, match='duplicate'):
        save_export(tmp_path, tmp_path / 'policy.json', exported)
    exported = snapshot()
    exported['settings']['id'] = 'different-instance'
    with pytest.raises(ValueError, match='shadow'):
        validate_export(exported)
    assert list(tmp_path.iterdir()) == []
