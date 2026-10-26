"""Host setup and the effective Compose deployment contract (no YAML text checks)."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not (ROOT / 'compose.yaml').is_file(),
                               reason='host-side deployment tests require a source checkout')


@pytest.mark.skipif(os.getuid() == 0, reason='setup is explicitly a host non-root operation')
def test_user_setup_preserves_settings_and_existing_output_permissions(tmp_path):
    root = tmp_path / 'project with spaces'
    (root / 'scripts').mkdir(parents=True)
    shutil.copy2(ROOT / 'scripts/setup-user.sh', root / 'scripts/setup-user.sh')
    shutil.copy2(ROOT / '.env.example', root / '.env.example')
    env = root / '.env'
    preserved = 'ROS_DOMAIN_ID=47\nTOKEN="literal $(touch SHOULD_NOT_EXIST)"\n# keep this comment\n'
    original = preserved + 'RC_UID=99999\nexport RC_GID=99999\n'
    env.write_text(original)
    (root / 'snapshot').mkdir(mode=0o777)
    old = root / 'snapshot/old.json'
    old.write_text('{"old": true}')
    old.chmod(0o600)
    before = [(p.stat().st_uid, p.stat().st_gid, stat.S_IMODE(p.stat().st_mode))
              for p in (old.parent, old)]

    command = ['sh', str(root / 'scripts/setup-user.sh')]
    first = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True)
    assert first.returncode == 0, first.stderr
    expected = env.read_text()
    assert expected.startswith(original)
    assert expected.endswith(f'RC_UID={os.getuid()}\nRC_GID={os.getgid()}\n')
    second = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True)
    assert second.returncode == 0, second.stderr
    assert env.read_text() == expected
    assert 'literal' not in first.stdout + first.stderr
    assert not list(tmp_path.rglob('SHOULD_NOT_EXIST'))
    assert before == [(p.stat().st_uid, p.stat().st_gid, stat.S_IMODE(p.stat().st_mode))
                      for p in (old.parent, old)]
    assert old.read_text() == '{"old": true}'
    for name in ('outputs', 'snapshot', 'models', 'cache/huggingface'):
        assert (root / name).is_dir()
        assert (root / name).stat().st_uid == os.getuid()


@pytest.mark.skipif(os.getuid() == 0, reason='setup is explicitly a host non-root operation')
def test_user_setup_creates_local_environment_from_example(tmp_path):
    (tmp_path / 'scripts').mkdir()
    shutil.copy2(ROOT / 'scripts/setup-user.sh', tmp_path / 'scripts/setup-user.sh')
    shutil.copy2(ROOT / '.env.example', tmp_path / '.env.example')
    result = subprocess.run(['sh', str(tmp_path / 'scripts/setup-user.sh')],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    content = (tmp_path / '.env').read_text()
    assert 'RC_MCP_PORT=4000' in content
    assert f'RC_UID={os.getuid()}\n' in content
    assert f'RC_GID={os.getgid()}\n' in content
    assert stat.S_IMODE((tmp_path / '.env').stat().st_mode) == 0o600


@pytest.mark.skipif(os.getuid() == 0, reason='setup is explicitly a host non-root operation')
def test_user_setup_preserves_multiline_values_and_compose_uses_new_identity(tmp_path):
    (tmp_path / 'scripts').mkdir()
    shutil.copy2(ROOT / 'scripts/setup-user.sh', tmp_path / 'scripts/setup-user.sh')
    content = ("TOKEN='line1\nRC_UID=token-content\nexport RC_GID=also-content\nline3'\n"
               'TEXT="second\nRC_GID=quoted-content\nlast"\n'
               'RC_UID=99999\nRC_GID=99999\n')
    env_file = tmp_path / '.env'
    env_file.write_text(content)
    for _ in range(2):
        result = subprocess.run(['sh', str(tmp_path / 'scripts/setup-user.sh')],
                                text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        assert "TOKEN='line1\nRC_UID=token-content\nexport RC_GID=also-content\nline3'" in env_file.read_text()
        assert 'TEXT="second\nRC_GID=quoted-content\nlast"' in env_file.read_text()
    docker = shutil.which('docker')
    if not docker:
        pytest.skip('value preservation checked; effective values require Docker Compose CLI')
    compose = tmp_path / 'probe.yaml'
    compose.write_text('services:\n  probe:\n    image: busybox:1.37.0\n'
                       '    user: "${RC_UID}:${RC_GID}"\n'
                       '    environment:\n      TOKEN: ${TOKEN}\n      TEXT: ${TEXT}\n')
    environment = {k: v for k, v in os.environ.items()
                   if k not in ('RC_UID', 'RC_GID', 'TOKEN', 'TEXT') and not k.startswith('COMPOSE_')}
    result = subprocess.run([docker, 'compose', '--env-file', str(env_file), '-f', str(compose),
                             'config', '--format', 'json'], env=environment,
                            text=True, capture_output=True, check=True)
    service = json.loads(result.stdout)['services']['probe']
    assert service['user'] == f'{os.getuid()}:{os.getgid()}'
    assert service['environment']['TOKEN'] == 'line1\nRC_UID=token-content\nexport RC_GID=also-content\nline3'
    assert service['environment']['TEXT'] == 'second\nRC_GID=quoted-content\nlast'


def compose_config(tmp_path, files, identity=True):
    docker = shutil.which('docker')
    if not docker:
        pytest.skip('Docker Compose CLI is required for effective config checks')
    env_file = tmp_path / 'empty.env'
    env_file.write_text('')
    environment = {k: v for k, v in os.environ.items() if not k.startswith(('RC_', 'COMPOSE_'))}
    if identity:
        environment.update(RC_UID='12345', RC_GID='23456')
    command = [docker, 'compose', '--env-file', str(env_file), '--profile', 'ros-sim']
    for file in ('compose.yaml', *files):
        command += ['-f', str(ROOT / file)]
    return subprocess.run([*command, 'config', '--format', 'json'], env=environment,
                          text=True, capture_output=True, timeout=20)


@pytest.mark.parametrize('files', [(), ('compose.ros-sim.yaml',), ('compose.acone.yaml',),
    ('compose.qwen.yaml',), ('compose.llm.yaml',),
    ('compose.qwen.yaml', 'compose.llm.yaml', 'compose.acone.yaml'),
    ('compose.test.yaml',), ('compose.test.yaml', 'compose.monitor-test.yaml'),
    ('compose.qwen.yaml', 'compose.qwen-test.yaml'),
    ('compose.llm.yaml', 'compose.llm-test.yaml')])
def test_compose_services_share_non_root_identity_and_writable_home(tmp_path, files):
    result = compose_config(tmp_path, files)
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)['services']
    for name, service in services.items():
        if name in ('rpc-init', 'llm-fixtures', 'qwen-fixtures'):
            continue  # One-shot ownership initialization, never business processes.
        assert service.get('user') == '12345:23456', name
        if name != 'ipc-anchor':
            assert service['environment']['HOME'] == '/tmp/robot-control', name
            assert '/tmp/robot-control:uid=12345,gid=23456,mode=0700' in service['tmpfs'], name
        if name.startswith('policy-'):
            assert service['depends_on']['rpc-init']['condition'] == 'service_completed_successfully'
        for volume in service.get('volumes', []):
            if volume['type'] == 'bind':
                assert volume['bind']['create_host_path'] is False, (name, volume)
    init = services['rpc-init']
    assert init['user'] == '0:0'
    assert [v['target'] for v in init['volumes']] == ['/run/robot-control']
    assert init['volumes'][0]['type'] == 'volume'


def test_compose_requires_explicit_host_identity(tmp_path):
    result = compose_config(tmp_path, (), identity=False)
    assert result.returncode != 0
    assert 'setup-user.sh' in result.stderr
