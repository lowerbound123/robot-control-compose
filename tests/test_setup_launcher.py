import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_setup_help_runs_from_another_directory_without_creating_an_environment(tmp_path):
    checkout = tmp_path / 'checkout with spaces'
    shutil.copytree(ROOT / 'scripts', checkout / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(ROOT / 'setup.sh', checkout)
    env = os.environ.copy()
    env.pop('SETUP_PYTHON', None)
    result = subprocess.run([str(checkout / 'setup.sh'), '--help'], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert '--config-only' in result.stdout and '--policies' in result.stdout
    assert not (checkout / '.setup').exists()


@pytest.mark.parametrize('installed_version', [None, '0.18.6'])
def test_missing_or_wrong_yaml_reports_manual_install_before_setup(tmp_path, monkeypatch, capsys, installed_version):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    import setup_project
    yaml = None
    if installed_version:
        yaml = ModuleType('ruamel.yaml')
        yaml.__version__ = installed_version
    monkeypatch.setitem(sys.modules, 'ruamel.yaml', yaml)
    monkeypatch.setattr(os, 'getuid', lambda: 1000)
    monkeypatch.setattr(setup_project, 'ROOT', tmp_path)
    monkeypatch.setattr(sys, 'argv', ['setup_project.py', '--config-only'])
    monkeypatch.setattr(setup_project, 'configuration_updates',
                        lambda root: pytest.fail('setup started before checking the YAML dependency'))
    with pytest.raises(SystemExit) as error:
        setup_project.main()
    assert error.value.code == 1
    message = capsys.readouterr().err
    assert 'ruamel.yaml==0.19.1' in message
    install = shlex.split(message.splitlines()[-1])
    assert install[:4] == [sys.executable, '-m', 'pip', 'install']
    assert '--require-hashes' in install and str(tmp_path / 'requirements/setup.lock') in install
    assert not (tmp_path / '.setup').exists()


def test_config_only_keeps_deployment_state_private_and_rejects_a_link(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    import setup_project
    monkeypatch.setattr(os, 'getuid', lambda: 1000)
    monkeypatch.setattr(setup_project, 'docker_endpoint', lambda root: ('/tmp/fixture.sock', 1000))
    (tmp_path / '.env.example').touch()
    (tmp_path / 'policy').mkdir()
    setup_project.setup(tmp_path, [], config_only=True)
    state = tmp_path / '.setup'
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / 'runtime.json').is_file()

    target = state.rename(tmp_path / 'saved-state')
    state.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        setup_project.setup(tmp_path, [], config_only=True)
