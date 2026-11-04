import importlib
import json

import pytest

from internvla_maas_helpers import PACKAGE, settings


def loader():
    assert (PACKAGE / 'rc_internvla_maas/configuration.py').is_file(), 'network configuration loader missing'
    return importlib.import_module('rc_internvla_maas.configuration').load_model_config


def test_complete_cold_config_resolves_without_weights_or_network(tmp_path):
    path = tmp_path / 'remote.json'
    original = settings('wss://model.example/base/')
    path.write_text(json.dumps(original))
    actual = loader()('remote.json', project_root=tmp_path)
    assert actual['service']['connect_timeout_s'] == 30
    assert actual['service']['close_timeout_s'] == 2
    assert actual['execution'] == dict(chunk_size=50, request_when_remaining=30,
                                     inference_timeout_s=60, expected_control_hz=30)
    assert actual['actions']['gripper_compensation'] is True
    assert actual['observations']['state_offsets'] == {}
    assert json.loads(path.read_text()) == original


@pytest.mark.parametrize('section,key,value', [
    ('service', 'address', 'https://example.org'),
    ('service', 'address', 'ws://user:password@example.org'),
    ('service', 'address', 'ws://@example.org'),
    ('service', 'address', 'ws://example.org?api_key=secret'),
    ('service', 'address', 'ws://example.org/#fragment'),
    ('service', 'address', 'ws://example.org:bad'),
    ('service', 'address', 'ws://example.org/../bad'),
    ('service', 'api_key', 'key\r\nInjected: value'),
    ('service', 'robot_id', ''), ('service', 'embodiment', ''),
    ('service', 'connect_timeout_s', 0), ('service', 'close_timeout_s', True),
    ('execution', 'chunk_size', 49), ('execution', 'request_when_remaining', 31),
    ('execution', 'inference_timeout_s', -1), ('execution', 'expected_control_hz', False),
    ('runtime', 'instruction', ' '), ('actions', 'gripper_compensation', 1),
    ('observations', 'state_offsets', {'14': 1}),
    ('observations', 'state_offsets', {'6': float('inf')}),
    ('service', 'array_encoding', 'openpi'),
])
def test_invalid_config_is_rejected_without_echoing_values(tmp_path, section, key, value):
    raw = settings()
    raw.setdefault(section, {})[key] = value
    raw['service']['api_key'] = value if key == 'api_key' else 'private-canary'
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError) as error:
        loader()(path, project_root=tmp_path)
    assert 'private-canary' not in str(error.value)
    assert 'Injected' not in str(error.value)


@pytest.mark.parametrize('content', ['{"api_key":"private-canary",',
                                       '{"service":{},"service":{}}', '[]'])
def test_invalid_json_is_sanitized(tmp_path, content):
    path = tmp_path / 'bad.json'
    path.write_text(content)
    with pytest.raises(ValueError) as error:
        loader()(path, project_root=tmp_path)
    assert 'private-canary' not in str(error.value)


def test_manifest_preserves_sync_cold_contract():
    loader()
    from rc_internvla_maas.configuration import validate_manifest
    good = dict(execution=dict(mode='sync', call_every_actions=1, control_mode='qpos'),
                params={'model_config_path': 'model.json'}, hot_reloadable=[])
    validate_manifest(good)
    for patch in ({'hot_reloadable': ['params.model_config_path']},
                  {'execution': dict(mode='async', call_every_actions=1, control_mode='qpos')}):
        with pytest.raises(ValueError):
            validate_manifest({**good, **patch})
