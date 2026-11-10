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


@pytest.mark.parametrize('suffix', ['.yaml', '.yml'])
def test_yaml_model_accepts_comments_multiline_instruction_and_jpeg_settings(tmp_path, suffix):
    path = tmp_path / ('model' + suffix)
    path.write_text('''# Private model configuration
service:
  address: wss://model.example/base/
  robot_id: test-robot
  api_key: private-canary
  embodiment: test-dual-arm
runtime:
  instruction: |-
    Pick up the object.
    Place it in the tray.
observations:
  jpeg: true
  jpeg_quality: 92
  state_offsets: {"0": 0.25}
''')
    actual = loader()(path.name, project_root=tmp_path)
    assert actual['runtime']['instruction'] == 'Pick up the object.\nPlace it in the tray.'
    assert actual['observations'] == {'jpeg': True, 'jpeg_quality': 92, 'state_offsets': {'0': .25}}
    assert actual['service']['api_key'] == 'private-canary'


def test_legacy_json_defaults_to_raw_with_a_valid_jpeg_quality(tmp_path):
    path = tmp_path / 'old.json'
    path.write_text(json.dumps(settings()))
    observations = loader()(path, project_root=tmp_path)['observations']
    assert observations['jpeg'] is False
    assert observations['jpeg_quality'] == 85


@pytest.mark.parametrize('quality', [85, 95, 100])
def test_jpeg_quality_boundaries_are_accepted(tmp_path, quality):
    raw = settings()
    raw['observations'] = {'jpeg': True, 'jpeg_quality': quality}
    path = tmp_path / 'model.yaml'
    # JSON is also valid YAML 1.2; block YAML is covered separately above.
    path.write_text(json.dumps(raw))
    assert loader()(path, project_root=tmp_path)['observations']['jpeg_quality'] == quality


@pytest.mark.parametrize('key,value', [
    ('jpeg', 'true'), ('jpeg', 1), ('jpeg', None),
    ('jpeg_quality', 84), ('jpeg_quality', 101), ('jpeg_quality', True),
    ('jpeg_quality', 85.0), ('jpeg_quality', '85'), ('jpeg_quality', None),
])
def test_jpeg_configuration_requires_strict_types_and_protocol_quality(tmp_path, key, value):
    raw = settings()
    raw['observations'] = {'jpeg': False, 'jpeg_quality': 85, key: value}
    path = tmp_path / 'model.yaml'
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match=key):
        loader()(path, project_root=tmp_path)


@pytest.mark.parametrize('content', [
    'service: {api_key: private-canary, api_key: other}\n',
    'service: {api_key: private-canary\n',
    'service: !!python/object/apply:os.system [private-canary]\n',
    'service: !!bool private-canary\n',
    'service: &secret {api_key: private-canary}\nruntime: &secret {}\n',
    '%YAML 1.1\n---\nservice: {api_key: private-canary}\n',
    'service: &secret {api_key: private-canary, cycle: *secret}\n',
    'service: {api_key: private-canary, timeout: .inf}\n',
    'service: {api_key: private-canary, day: 2026-11-10}\n',
    'service: {api_key: private-canary, 1: value}\n',
    'service: {}\n---\nservice: {api_key: private-canary}\n',
])
def test_yaml_errors_and_warnings_do_not_expose_private_values(tmp_path, content, recwarn, capsys):
    path = tmp_path / 'bad.yaml'
    path.write_text(content)
    with pytest.raises(ValueError) as error:
        loader()(path, project_root=tmp_path)
    assert 'private-canary' not in str(error.value)
    assert not recwarn
    assert 'private-canary' not in capsys.readouterr().err


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


@pytest.mark.parametrize('mode,interval', [('sync', 1), ('async', 1), ('async', 5), ('sync', 50)])
def test_manifest_allows_standard_scheduling_and_hot_execution_fields(mode, interval):
    loader()
    from rc_internvla_maas.configuration import validate_manifest
    good = dict(execution=dict(mode=mode, call_every_actions=interval, control_mode='qpos'),
                params={'model_config_path': 'model.json'},
                hot_reloadable=['execution.mode', 'execution.call_every_actions'])
    validate_manifest(good)


@pytest.mark.parametrize('patch', [
    {'hot_reloadable': ['params.model_config_path']},
    {'execution': dict(mode='async', call_every_actions=1, control_mode='epos')},
])
def test_manifest_keeps_actual_joint_space_and_cold_model_binding(patch):
    loader()
    from rc_internvla_maas.configuration import validate_manifest
    good = dict(execution=dict(mode='async', call_every_actions=1, control_mode='qpos'),
                params={'model_config_path': 'model.json'}, hot_reloadable=[])
    with pytest.raises(ValueError):
        validate_manifest({**good, **patch})
