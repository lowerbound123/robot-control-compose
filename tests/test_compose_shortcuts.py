import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / 'checkout with spaces'
    shutil.copytree(ROOT / 'scripts', root / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
    for name in ('src', 'packages'):
        (root / name).symlink_to(ROOT / name, target_is_directory=True)
    for path in ROOT.glob('compose*.yaml'):
        shutil.copy2(path, root / path.name)
    (root / '.setup').mkdir()
    (root / '.setup/runtime.json').write_text(json.dumps({
        'version': 3, 'base_project': 'shortcut-test', 'uid': 1234, 'gid': 5678,
        'deployment_id': 'shortcut-deployment', 'daemon_id': 'test-daemon',
    }))
    (root / '.env').write_text('')
    (root / '.setup/base.env').write_text(
        'RC_BASE_PROJECT=shortcut-test\nRC_UID=1234\nRC_GID=5678\n'
        'RC_DOCKER_GID=0\nRC_DOCKER_SOCKET=/var/run/docker.sock\nRC_RPC_VOLUME=shortcut-rpc\n'
        'RC_CONNECTOR_RPC_VOLUME=shortcut-connector-rpc\n'
    )
    folder = root / 'policy/example'
    folder.mkdir(parents=True)
    shutil.copy2(ROOT / 'policy/example/compose.yaml', folder / 'compose.yaml')
    for source in ('mock', 'acone'):
        folder = root / 'connector' / source
        folder.mkdir(parents=True)
        shutil.copy2(ROOT / 'connector' / source / 'compose.yaml', folder / 'compose.yaml')
    return root


@pytest.fixture
def docker_log(tmp_path, monkeypatch):
    """Use the real Compose config parser and simulate an empty daemon for deployment commands."""
    docker = shutil.which('docker')
    if not docker:
        pytest.skip('Docker CLI required for Compose config validation')
    directory = tmp_path / 'bin'
    directory.mkdir()
    log = tmp_path / 'docker.jsonl'
    wrapper = directory / 'docker'
    wrapper.write_text(f'#!{sys.executable}\nREAL_DOCKER = {docker!r}\nLOG = {str(log)!r}\n' + '''
import json, os, sys
from pathlib import Path
if 'config' in sys.argv[1:]:
    os.execv(REAL_DOCKER, [REAL_DOCKER, *sys.argv[1:]])
if sys.argv[1] == 'info':
    print('test-daemon')
record = {
    'arguments': sys.argv[1:], 'cwd': os.getcwd(),
    'env': {key: os.environ.get(key) for key in ('RC_HOST_PROJECT_ROOT', 'RC_UID', 'RC_GID')},
}
with Path(LOG).open('a') as stream:
    print(json.dumps(record), file=stream)
if os.environ.get('DOCKER_FAIL_ACTION') in sys.argv[1:]:
    sys.exit(23)
''')
    wrapper.chmod(0o755)
    monkeypatch.setenv('PATH', str(directory) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('SETUP_PYTHON', sys.executable)
    return log


def invoke(root, action, *arguments):
    return subprocess.run([str(root / 'scripts' / (action + '.sh')), *arguments],
                          cwd=root.parent, text=True, capture_output=True)


def files(arguments):
    return [arguments[i + 1] for i, value in enumerate(arguments) if value == '-f']


def records(log):
    return [json.loads(line) for line in log.read_text().splitlines()]


def test_up_combines_files_in_order_and_handles_spaces(checkout, docker_log):
    overlay = checkout / 'compose extra.yaml'
    overlay.write_text('services: {}\n')
    result = invoke(checkout, 'up', 'compose.yaml', 'compose.acone.yaml', str(overlay), 'compose.webui.yaml')
    assert result.returncode == 0, result.stderr
    commands = [r for r in records(docker_log) if r['arguments'][0] == 'compose']
    stop, down, up = (r['arguments'] for r in commands)
    assert 'stop' in stop and stop[-1] == 'robot-control'
    assert down[-2:] == ['down', '--remove-orphans']
    assert up[-2:] == ['up', '-d']
    for record in commands:
        assert files(record['arguments']) == [str(checkout / name) for name in (
            'compose.yaml', 'compose.acone.yaml', 'compose extra.yaml', 'compose.webui.yaml')]
        assert record['cwd'] == str(checkout)


@pytest.mark.parametrize('action,tail', [('up', ['up', '-d']), ('attach', ['attach', 'robot-control'])])
def test_default_entry_and_docker_exit_status(checkout, docker_log, monkeypatch, action, tail):
    monkeypatch.setenv('DOCKER_FAIL_ACTION', action)
    result = invoke(checkout, action)
    assert result.returncode == 23, result.stderr
    command = records(docker_log)[-1]['arguments']
    assert files(command) == [str(checkout / 'compose.yaml')]
    assert command[-2:] == tail


def test_up_stops_on_unsuccessful_cleanup(checkout, monkeypatch):
    import compose_shortcuts
    monkeypatch.chdir(checkout.parent)
    monkeypatch.setattr(compose_shortcuts.compose_project, 'down', lambda root, args: 1)
    monkeypatch.setattr(os, 'execvpe', lambda *args: pytest.fail('started after unsuccessful down'))
    assert compose_shortcuts.main(['up'], root=checkout) == 1


@pytest.mark.parametrize('action,args', [
    ('up', ['missing.yaml']), ('up', ['--build']), ('build', []),
    ('build', ['rc', 'webui']), ('down', ['webui']), ('attach', ['webui']),
])
def test_invalid_arguments_do_not_operate_on_docker(checkout, docker_log, action, args):
    result = invoke(checkout, action, *args)
    assert result.returncode != 0
    assert not docker_log.exists()
    assert 'Traceback' not in result.stderr
    assert 'usage:' in result.stderr or f'{action} error:' in result.stderr


@pytest.mark.parametrize('target,service,overlay', [
    ('rc', 'robot-control', None), ('robot-control', 'robot-control', None),
    ('ipc-anchor', 'ipc-anchor', None), ('webui', 'webui', 'compose.webui.yaml'),
])
def test_build_finds_only_target_service(checkout, docker_log, target, service, overlay):
    result = invoke(checkout, 'build', target)
    assert result.returncode == 0, result.stderr
    command = records(docker_log)[-1]['arguments']
    assert command[-2:] == ['build', service]
    expected = ['compose.yaml'] + ([overlay] if overlay else [])
    assert files(command) == [str(checkout / name) for name in expected]


def test_build_policy_uses_own_project_and_environment(checkout, docker_log):
    (checkout / 'policy/example/deployment.env').write_text('EXAMPLE_BUILD=value\n')
    result = invoke(checkout, 'build', 'example')
    assert result.returncode == 0, result.stderr
    record = records(docker_log)[-1]
    command = record['arguments']
    assert command[-2:] == ['build', 'policy']
    assert files(command) == [str(checkout / 'policy/example/compose.yaml')]
    assert command[command.index('--project-directory') + 1] == str(checkout / 'policy/example')
    assert command[command.index('--env-file') + 1] == str(checkout / 'policy/example/deployment.env')
    assert record['env'] == {'RC_HOST_PROJECT_ROOT': str(checkout), 'RC_UID': '1234', 'RC_GID': '5678'}


@pytest.mark.parametrize('target', ['unknown', 'ros-sim', '../example'])
def test_unknown_or_unbuildable_target_never_builds_all(checkout, docker_log, target):
    result = invoke(checkout, 'build', target)
    assert result.returncode != 0
    assert not docker_log.exists()
    assert 'Traceback' not in result.stderr
    assert 'build error:' in result.stderr


def test_ambiguous_build_does_not_choose_an_arbitrary_overlay(checkout, docker_log):
    shutil.copy2(checkout / 'compose.webui.yaml', checkout / 'compose.other.yaml')
    result = invoke(checkout, 'build', 'webui')
    assert result.returncode != 0
    assert not docker_log.exists()
    assert 'compose.webui.yaml' in result.stderr and 'compose.other.yaml' in result.stderr


def test_down_uses_owner_aware_cleanup_and_removes_overlay_containers(checkout, monkeypatch):
    import compose_shortcuts
    import compose_project
    calls = []
    def down(root, arguments):
        calls.append((root, arguments))
        return 1  # Forced cleanup must remain a failure through the shortcut.
    monkeypatch.setattr(compose_project, 'down', down)
    assert compose_shortcuts.main(['down'], root=checkout) == 1
    assert calls == [(checkout, ['down', '--remove-orphans'])]


@pytest.mark.parametrize('source', ['mock', 'acone'])
def test_connector_build_is_explicit_and_uses_only_its_service(checkout, docker_log, source):
    # A matching policy source remains independently addressable without a prefix.
    policy = checkout / 'policy' / source
    policy.mkdir()
    shutil.copy2(checkout / 'policy/example/compose.yaml', policy / 'compose.yaml')
    result = invoke(checkout, 'build', 'connector:' + source)
    assert result.returncode == 0, result.stderr
    record = records(docker_log)[-1]
    command = record['arguments']
    assert command[-2:] == ['build', 'connector']
    assert files(command) == [str(checkout / 'connector' / source / 'compose.yaml')]
    assert command[command.index('--project-directory') + 1] == str(checkout / 'connector' / source)
    assert command[command.index('--env-file') + 1] == '/dev/null'
    assert record['env'] == {'RC_HOST_PROJECT_ROOT': str(checkout), 'RC_UID': '1234', 'RC_GID': '5678'}
    result = invoke(checkout, 'build', source)
    assert result.returncode == 0, result.stderr
    assert records(docker_log)[-1]['arguments'][-2:] == ['build', 'policy']


@pytest.mark.parametrize('target', ['connector:', 'connector:../mock', 'connector:missing', 'connector:mock/extra', 'mock'])
def test_invalid_connector_target_never_builds_other_services(checkout, docker_log, target):
    result = invoke(checkout, 'build', target)
    assert result.returncode != 0
    assert not docker_log.exists()
    assert 'Traceback' not in result.stderr


def test_connector_compose_with_sidecar_is_not_a_single_container_build(checkout, docker_log):
    (checkout / 'connector/mock/compose.yaml').write_text('services: {connector: {build: .}, sidecar: {build: .}}\n')
    result = invoke(checkout, 'build', 'connector:mock')
    assert result.returncode != 0
    assert not docker_log.exists()
    assert 'one buildable service named connector' in result.stderr
