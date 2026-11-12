"""Run the one-time migration against private configurations in a temporary checkout."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from config_helpers import read_config


ROOT = Path(__file__).resolve().parents[1]
DEEPSEEK = 'provider.yaml'
CHOICE = 'providers/gpt-decision-choice.yaml'
PREDICATE = 'providers/gpt-decision-predicate.yaml'
SCORE = 'providers/gpt-decision-score.yaml'
FACTORY = (DEEPSEEK, CHOICE, PREDICATE, SCORE)
OLD = ('type: deepseek\napi_key: local-private-key\nmodel: special\n'
       'base_url: https://local.test\ntimeout_s: 9\n')


@pytest.fixture
def folder(tmp_path):
    folder = tmp_path / 'policy/llm_failure'
    (folder / 'providers').mkdir(parents=True)
    for profile in FACTORY:
        example = profile + '.example'
        shutil.copyfile(ROOT / 'policy/llm_failure' / example, folder / example)
    (folder / 'policy.yaml').write_text(
        '# private manifest\nparams:\n  task: 移动蓝块\n  prompt: |-\n    观察夹爪\n    确认抓取\n'
        '  history_size: 4  # keep tuning\n  provider_config_path: provider.yaml\n')
    return folder


def migrate(root, *options):
    return subprocess.run(
        [sys.executable, str(ROOT / 'policy/llm_failure/scripts/migrate_prompts.py'), '--root', str(root), *options],
        capture_output=True, text=True, check=False)


@pytest.mark.parametrize('kind,expected', [
    ('openai_decisions', 'openai_decisions'),
    ('gpt-decision-choice', 'choice_descriptions'),
])
def test_old_decision_fields_require_manual_update_before_any_write(tmp_path, folder, kind, expected):
    candidate = folder / 'local/retired.yml'
    candidate.parent.mkdir()
    candidate.write_text(OLD.replace('deepseek', kind) + 'choice_descriptions: {failed: 掉落, not_failed: 继续}\n')
    before = {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = migrate(tmp_path)
    assert result.returncode != 0
    assert candidate.name in result.stderr and expected in result.stderr and '手动' in result.stderr
    assert 'local-private-key' not in result.stdout + result.stderr
    assert before == {p: p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_migrate_private_and_new_profiles_then_repeat_byte_for_byte(tmp_path, folder):
    manifest = folder / 'policy.yaml'
    selected = 'cell/shift-a.yml'
    manifest.write_text(manifest.read_text().replace('provider_config_path: provider.yaml',
                                                    'provider_config_path: ' + selected))
    (folder / 'cell').mkdir()
    (folder / selected).write_text(
        '# night shift\ntype: gpt-decision-choice\nmodel: custom-model  # keep this note\n'
        'api_key: key-night-shift\nbase_url: https://local.test/v1\ntimeout_s: 7\n'
        'task: 检查红块\nprompt: 只观察左手\nresponse_instructions: 本班判断方式\n'
        "input_templates:\n  camera: '观察 {camera}'  # calibrated\n"
        'question_name: dropped\nchoices:\n'
        '  - {value: lost, description: 红块脱落, failed: true}\n'
        '  - {value: held, description: 红块仍在夹爪中, failed: false}\n')
    # Setup may already have seeded the new template, including its empty task.
    shutil.copyfile(folder / (DEEPSEEK + '.example'), folder / DEEPSEEK)
    spare = 'cache/spare.yaml'  # Only included when explicitly requested.
    (folder / 'cache').mkdir()
    for relative in ('providers/bench.yml', spare):
        (folder / relative).write_text(OLD)
    originals = {p: p.read_bytes() for p in
                 (manifest, folder / selected, folder / DEEPSEEK, folder / spare, folder / 'providers/bench.yml')}

    result = migrate(tmp_path, '--profile', spare)
    assert result.returncode == 0, result.stderr
    assert 'local-private-key' not in result.stdout + result.stderr
    assert read_config(manifest)['params'] == {'history_size': 4, 'provider_config_path': selected}
    assert '# private manifest' in manifest.read_text() and '# keep tuning' in manifest.read_text()
    for profile in FACTORY:
        template = read_config(folder / (profile + '.example'))
        assert read_config(folder / profile) == {**template, 'task': '移动蓝块', 'prompt': '观察夹爪\n确认抓取'}
        assert (folder / profile).stat().st_mode & 0o777 == 0o600
    for profile in ('providers/bench.yml', spare):
        assert read_config(folder / profile) == {
            **read_config(folder / DEEPSEEK), 'model': 'special', 'api_key': 'local-private-key',
            'base_url': 'https://local.test', 'timeout_s': 9}
    custom = read_config(folder / selected)
    example = read_config(folder / (CHOICE + '.example'))
    assert custom == {
        'type': 'gpt-decision-choice', 'model': 'custom-model', 'api_key': 'key-night-shift',
        'base_url': 'https://local.test/v1', 'timeout_s': 7, 'task': '检查红块', 'prompt': '只观察左手',
        'response_instructions': '本班判断方式', 'input_templates': {**example['input_templates'], 'camera': '观察 {camera}'},
        'question_name': 'dropped', 'choices': [{'value': 'lost', 'description': '红块脱落', 'failed': True},
                                               {'value': 'held', 'description': '红块仍在夹爪中', 'failed': False}]}
    assert all(note in (folder / selected).read_text() for note in ('# night shift', '# keep this note', '# calibrated'))
    backup, = (tmp_path / '.setup/backups').iterdir()
    for path, original in originals.items():
        saved = backup / path.relative_to(tmp_path)
        assert saved.read_bytes() == original
        assert (saved.stat().st_mode & 0o777) == (path.stat().st_mode & 0o777) == 0o600
    times = {p: p.stat().st_mtime_ns for p in tmp_path.rglob('*') if p.is_file()}
    assert migrate(tmp_path, '--profile', spare).returncode == 0
    assert times == {p: p.stat().st_mtime_ns for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('profile,question', [
    (PREDICATE, {'question_name': 'on_track', 'fail_if': {'op': '<=', 'threshold': 0.25}}),
    (SCORE, {'question_name': 'progress', 'levels': [
        {'label': 'lost', 'description': '夹爪已丢失目标'},
        {'label': 'held', 'description': '目标仍在夹爪中'}], 'fail_if': {'op': '<=', 'threshold': 0.5}}),
])
def test_migrate_keeps_private_decision_rules(tmp_path, folder, profile, question):
    settings = {**read_config(folder / (profile + '.example')), **question}
    del settings['task'], settings['prompt']
    path = folder / profile
    path.write_text(json.dumps(settings, ensure_ascii=False))
    result = migrate(tmp_path)
    assert result.returncode == 0, result.stderr
    assert read_config(path) == {**settings, 'task': '移动蓝块', 'prompt': '观察夹爪\n确认抓取'}


@pytest.mark.parametrize('invalid', [
    OLD.replace('deepseek', 'private-canary'), OLD + 'input_templates: private-canary\n',
    OLD + "input_templates: {task: 'Task {private-canary}'}\n",
    OLD.replace('deepseek', 'gpt-decision-choice'),
    OLD.replace('deepseek', 'gpt-decision-choice') + 'question_name: result\nchoices: [private-canary]\n',
    OLD.replace('deepseek', 'gpt-decision-predicate') + "question_name: result\nfail_if: {op: '>=', threshold: 1.1}\n",
    OLD.replace('deepseek', 'gpt-decision-score') + (
        "question_name: result\nlevels: [private-canary]\nfail_if: {op: '>=', threshold: 0}\n"),
    OLD.replace('local-private-key', '!!bool private-canary'),
    OLD + 'promt: private-canary\n', OLD.replace('api_key: local-private-key\n', ''),
])
def test_validation_precedes_all_writes_and_redacts_values(tmp_path, folder, invalid):
    candidate = folder / 'providers/invalid.yml'
    candidate.write_text(invalid)
    original = (folder / 'policy.yaml').read_bytes()
    result = migrate(tmp_path)
    assert result.returncode != 0 and '迁移' in result.stderr
    assert 'private-canary' not in result.stderr + result.stdout
    assert candidate.read_text() == invalid and (folder / 'policy.yaml').read_bytes() == original
    assert not (folder / DEEPSEEK).exists() and not (tmp_path / '.setup').exists()


def test_migration_preserves_disabled_suffix_and_accepts_an_unconfigured_task(tmp_path, folder):
    manifest = folder / 'policy.yaml'
    manifest.write_text(manifest.read_text().replace('task: 移动蓝块', "task: ''"))
    (folder / DEEPSEEK).write_text(
        'type: deepseek\napi_key: key-local\nmodel: custom\nbase_url: https://local.test\ntimeout_s: 6\n'
        "response_instructions: ''  # intentionally disabled\n")
    result = migrate(tmp_path)
    assert result.returncode == 0, result.stderr
    assert read_config(folder / DEEPSEEK)['response_instructions'] == ''
    assert read_config(folder / DEEPSEEK)['task'] == ''
    assert '# intentionally disabled' in (folder / DEEPSEEK).read_text()
    assert 'task' not in read_config(manifest)['params']


def test_legacy_text_reaches_unselected_local_profiles_and_container_mount_paths(tmp_path, folder):
    profiles = ('alternate.yaml', 'providers/night/alternate.yml', 'local/cells/a.yml', 'custom/day/backup.yaml')
    for filename in profiles:
        profile = folder / filename
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text(OLD)
    selected = '/workspace/policy/llm_failure/local/cells/a.yml'
    manifest = folder / 'policy.yaml'
    manifest.write_text(manifest.read_text().replace('provider_config_path: provider.yaml',
                                                     'provider_config_path: ' + selected))
    unrelated = {'compose.yaml': 'services: !some-type unchanged\n', 'labels.yml': 'sensors: [camera]\n',
                 'vendor/data.yml': 'invalid: [\n', '.runtime/private.yaml': 'type: deepseek\n'}
    for name, content in unrelated.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    result = migrate(tmp_path)
    assert result.returncode == 0, result.stderr
    assert read_config(manifest)['params']['provider_config_path'] == selected
    for name in profiles:
        data = read_config(folder / name)
        assert (data['task'], data['prompt'], data['api_key']) == ('移动蓝块', '观察夹爪\n确认抓取', 'local-private-key')
    assert all((folder / name).read_text() == content for name, content in unrelated.items())


@pytest.mark.parametrize('location', ['absolute', 'parent', 'symlink-file', 'symlink-parent', 'symlink-backup'])
def test_external_and_symlink_locations_require_manual_migration(tmp_path, folder, location):
    outside = tmp_path / 'outside'
    outside.mkdir()
    other = outside / 'secret.yml'
    other.write_text('type: deepseek\napi_key: example-only\n')
    manifest = folder / 'policy.yaml'
    selected = {
        'absolute': str(other), 'parent': '../../outside/secret.yml', 'symlink-file': 'provider.yaml',
        'symlink-parent': 'import/secret.yml', 'symlink-backup': 'provider.yaml',
    }[location]
    manifest.write_text(manifest.read_text().replace('provider_config_path: provider.yaml',
                                                     'provider_config_path: ' + selected))
    if location == 'symlink-file':
        (folder / 'providers/external.yml').symlink_to(other)
    elif location == 'symlink-parent':
        (folder / 'import').symlink_to(outside, target_is_directory=True)
    elif location == 'symlink-backup':
        (tmp_path / '.setup').symlink_to(outside, target_is_directory=True)
    before = manifest.read_bytes()
    result = migrate(tmp_path)
    assert result.returncode != 0 and '手动' in result.stderr
    assert manifest.read_bytes() == before and not (folder / DEEPSEEK).exists()
    assert list(outside.iterdir()) == [other] and other.read_text() == 'type: deepseek\napi_key: example-only\n'


def test_retry_after_write_failure_keeps_legacy_fields_until_every_profile_is_ready(tmp_path, folder, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    import config_files
    monkeypatch.syspath_prepend(str(ROOT / 'policy/llm_failure/scripts'))
    import migrate_prompts as migrate_llm_failure_prompts

    write = config_files.atomic_bytes

    def fail_score(path, data, *args, **kwargs):
        if path == folder / SCORE:
            raise OSError('injected interrupt')
        write(path, data, *args, **kwargs)

    monkeypatch.setattr(config_files, 'atomic_bytes', fail_score)
    before = (folder / 'policy.yaml').read_bytes()
    with pytest.raises(OSError):
        migrate_llm_failure_prompts.migrate(tmp_path)
    assert (folder / 'policy.yaml').read_bytes() == before
    monkeypatch.setattr(config_files, 'atomic_bytes', write)
    migrate_llm_failure_prompts.migrate(tmp_path)
    assert read_config(folder / SCORE)['task'] == '移动蓝块'
    assert 'task' not in read_config(folder / 'policy.yaml')['params']


def test_file_created_during_migration_keeps_its_private_edits_for_retry(tmp_path, folder, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    import config_files
    monkeypatch.syspath_prepend(str(ROOT / 'policy/llm_failure/scripts'))
    import migrate_prompts as migrate_llm_failure_prompts

    new_profile = folder / DEEPSEEK
    before = (folder / 'policy.yaml').read_bytes()
    write = config_files.atomic_bytes
    private = (OLD + 'task: 夜班只看左臂\n').encode()

    def concurrent_creation(path, data, *args, **kwargs):
        if path == new_profile and not path.exists():
            write(path, private, overwrite=False)
        return write(path, data, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(config_files, 'atomic_bytes', concurrent_creation)
        with pytest.raises(ValueError, match='重试'):
            migrate_llm_failure_prompts.migrate(tmp_path)
    assert new_profile.read_bytes() == private and (folder / 'policy.yaml').read_bytes() == before
    migrate_llm_failure_prompts.migrate(tmp_path)
    assert read_config(new_profile)['task'] == '夜班只看左臂'
    assert read_config(new_profile)['api_key'] == 'local-private-key'


def test_concurrent_edit_before_commit_keeps_the_new_credential(tmp_path, folder, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    monkeypatch.syspath_prepend(str(ROOT / 'policy/llm_failure/scripts'))
    import migrate_prompts as migrate_llm_failure_prompts

    profile, manifest = folder / DEEPSEEK, folder / 'policy.yaml'
    profile.write_text(OLD)
    before = manifest.read_bytes()
    render = migrate_llm_failure_prompts.render_yaml_config

    def edit_after_preparation(path, *args, **kwargs):
        rendered = render(path, *args, **kwargs)
        if path == manifest:
            profile.write_text(OLD.replace('local-private-key', 'rotated-key'))
        return rendered

    monkeypatch.setattr(migrate_llm_failure_prompts, 'render_yaml_config', edit_after_preparation)
    with pytest.raises(ValueError, match='重试'):
        migrate_llm_failure_prompts.migrate(tmp_path)
    assert read_config(profile)['api_key'] == 'rotated-key' and manifest.read_bytes() == before
