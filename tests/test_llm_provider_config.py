"""Exercise the shipped manifests, setup and worker command handlers together."""
import importlib.util
import json
from pathlib import Path

import pytest

from config_helpers import read_config
from rc_policy_sdk.commands import command_specs, resolve_handlers


ROOT = Path(__file__).resolve().parents[1]
PROFILES = [
    ('llm_direction', 'provider.yaml'),
    ('llm_direction', 'providers/openai.yaml'),
    ('llm_direction', 'providers/openai_compatible.yaml'),
    ('llm_direction', 'providers/openai_decisions.yaml'),
    ('llm_failure', 'provider.yaml'),
    ('llm_failure', 'providers/openai_decisions.yaml'),
]


def prepare(tmp_path, policy, relative, monkeypatch):
    source = ROOT / 'policy' / policy
    folder = tmp_path / 'policy' / policy
    template = Path(relative + '.example')
    (folder / template).parent.mkdir(parents=True)
    for name in ('main.py', 'policy.yaml.example', template):
        (folder / name).write_bytes((source / name).read_bytes())
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    from setup_project import setup
    # Supply local deployment metadata; config-only setup still prepares all files.
    monkeypatch.setattr('setup_project.os.getuid', lambda: 1000)
    monkeypatch.setattr('setup_project.prepare_context', lambda *args, **kwargs: dict(
        uid=1000, gid=1000, deployment_id='test', base_project='test', rpc_volume='rpc',
        docker_socket='/tmp/test.sock', docker_gid=1000))
    (tmp_path / '.env.example').touch()
    setup(tmp_path, [], config_only=True)
    monkeypatch.syspath_prepend(str(ROOT / 'packages' / (policy.replace('_', '-') + '-policy') / 'src'))
    spec = importlib.util.spec_from_file_location(policy + '_entry', folder / 'main.py')
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    config = read_config(folder / 'policy.yaml')
    commands = command_specs(config)
    assert commands['switch-provider']['effect'] == 'reload_policy'
    return folder, resolve_handlers(entry, commands)['switch-provider']


@pytest.mark.parametrize('policy,relative', PROFILES)
def test_setup_creates_private_profiles_only_once_and_handler_returns_only_path(tmp_path, monkeypatch, policy, relative):
    folder, switch = prepare(tmp_path, policy, relative, monkeypatch)
    path = folder / relative
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match='api_key'):
        switch(None, relative)
    settings = {**read_config(path), 'api_key': 'local-test-credential'}
    if policy == 'llm_failure':
        settings['task'] = 'pick up the block'
    private = '# user comment\n' + json.dumps(settings)
    path.write_text(private)
    from setup_project import setup
    setup(tmp_path, [], config_only=True)
    assert path.read_text() == private
    assert switch(None, relative) == {'params.provider_config_path': relative}
    assert switch(None, str(path)) == {'params.provider_config_path': str(path)}


@pytest.mark.parametrize('raw', [
    'api_key: [local-test-credential\n',
    'api_key: !!bool local-test-credential\n',
    'api_key: &local-test-credential A\nmodel: &local-test-credential B\n',
    '%YAML 1.1\n---\napi_key: 12345e+6\n',
])
def test_invalid_yaml_does_not_quote_private_source(tmp_path, monkeypatch, recwarn, raw):
    folder, switch = prepare(tmp_path, 'llm_failure', 'provider.yaml', monkeypatch)
    path = folder / 'broken.yaml'
    path.write_text(raw)
    with pytest.raises(ValueError) as error:
        switch(None, 'broken.yaml')
    assert path.name in str(error.value)
    output = str(error.value) + str([str(w.message) for w in recwarn])
    assert 'local-test-credential' not in output and '12345e+6' not in output


def test_copying_entire_policy_tree_excludes_custom_private_profiles(tmp_path):
    from project_templates import copy_templates
    source = tmp_path / 'policy'
    folder = source / 'llm_failure' / 'providers'
    folder.mkdir(parents=True)
    (folder / 'private.yaml').write_text('api_key: local-only')
    (folder / 'public.yaml.example').write_text("api_key: ''")
    deployed = copy_templates(source, tmp_path / 'deployed') / 'llm_failure' / 'providers'
    assert not (deployed / 'private.yaml').exists()
    assert (deployed / 'public.yaml').is_file()
