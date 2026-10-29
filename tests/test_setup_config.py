import importlib.util
import json
from pathlib import Path
import stat

import pytest

ROOT = Path(__file__).resolve().parents[1]


def helpers():
    spec = importlib.util.spec_from_file_location('setup_config_files', ROOT / 'scripts/config_files.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deep_merge_preserves_user_values_and_whole_lists():
    merge = helpers().merge_json
    old = {'nested': {'secret': 'own', 'off': False, 'zero': 0, 'empty': '', 'null': None}, 'list': [9]}
    template = {'nested': {'secret': '', 'off': True, 'zero': 3, 'empty': 'default', 'null': {}, 'new': 2}, 'list': [1, 2]}
    value = merge(template, old)
    assert value['nested'] == {**old['nested'], 'new': 2}
    assert value['list'] == [9]
    assert 'new' not in old['nested']


def test_configuration_updates_validate_all_files_before_writes(tmp_path):
    module = helpers()
    (tmp_path / 'rc.json.example').write_text('{"a": 1}')
    (tmp_path / 'configs').mkdir()
    (tmp_path / 'configs/rc.mock.json.example').write_text('{"nested": {}}')
    current = tmp_path / 'configs/rc.mock.json'
    current.write_text('{"nested":"private-do-not-print"}')
    with pytest.raises(ValueError) as error:
        module.configuration_updates(tmp_path)
    assert 'private-do-not-print' not in str(error.value)
    assert not (tmp_path / 'rc.json').exists()
    assert current.read_text() == '{"nested":"private-do-not-print"}'


def test_merge_backs_up_and_is_idempotent(tmp_path):
    module = helpers()
    folder = tmp_path / 'policy/llm_direction'
    folder.mkdir(parents=True)
    template = {'params': {'provider': {'api_key': '', 'model': 'new'}}}
    (folder / 'policy.json.example').write_text(json.dumps(template))
    path = folder / 'policy.json'
    original = '{"params":{"provider":{"api_key":"local-secret"}}}'
    path.write_text(original)
    changes, docs = module.configuration_updates(tmp_path)
    module.write_updates(tmp_path, changes)
    assert json.loads(path.read_text())['params']['provider'] == {'api_key': 'local-secret', 'model': 'new'}
    backups = list((tmp_path / '.setup/backups').rglob('policy.json'))
    assert len(backups) == 1 and backups[0].read_text() == original
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    before = path.stat().st_mtime_ns
    changes, _ = module.configuration_updates(tmp_path)
    assert not changes
    module.write_updates(tmp_path, changes)
    assert path.stat().st_mtime_ns == before


@pytest.mark.parametrize('data', ['{"api_key":"secret",', '{"x":1,"x":2}', '{"x":NaN}'])
def test_bad_json_does_not_reveal_values(data, tmp_path):
    module = helpers()
    (tmp_path / 'rc.json.example').write_text('{"x":1}')
    (tmp_path / 'rc.json').write_text(data)
    with pytest.raises(ValueError) as error:
        module.configuration_updates(tmp_path)
    assert 'secret' not in str(error.value)
    assert (tmp_path / 'rc.json').read_text() == data


def test_rc_env_merge_keeps_multiline_comments_and_never_executes(tmp_path):
    module = helpers()
    old = "# retain\nTOKEN='line1\nROS_DOMAIN_ID=inside-token\nline3'\nTEXT=\"$(touch NEVER)\"\nRC_UID=9\n"
    example = 'RC_UID=\nRC_GID=\nROS_DOMAIN_ID=83\n'
    result = module.merge_rc_env(example, old, 123, 456)
    assert result.startswith(old)
    values = module.parse_env(result)
    assert values['TOKEN'] == 'line1\nROS_DOMAIN_ID=inside-token\nline3'
    assert values['TEXT'] == '$(touch NEVER)'
    assert values['ROS_DOMAIN_ID'] == '83'
    assert (values['RC_UID'], values['RC_GID']) == ('123', '456')
    assert module.merge_rc_env(example, result, 123, 456) == result


def test_private_config_symlink_is_not_followed(tmp_path):
    module = helpers()
    outside = tmp_path / 'elsewhere'
    outside.write_text('{"x":1}')
    (tmp_path / 'rc.json.example').write_text('{"x":1}')
    (tmp_path / 'rc.json').symlink_to(outside)
    with pytest.raises(ValueError, match='symlink'):
        module.configuration_updates(tmp_path)
