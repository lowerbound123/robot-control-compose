import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def bootstrap():
    spec = importlib.util.spec_from_file_location('setup_bootstrap_test', ROOT/'scripts/setup_bootstrap.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tool_environment_is_locked_cached_and_repaired_after_failed_install(tmp_path):
    (tmp_path/'requirements').mkdir()
    lock=tmp_path/'requirements/setup.lock'
    lock.write_text('fixture-lock')
    calls=[]
    fail=[True]
    def run(command, **kwargs):
        calls.append(command)
        if command[1:3] == ['-m','venv']:
            target=Path(command[3]); (target/'bin').mkdir(exist_ok=True)
            (target/'bin/python').touch()
        if 'install' in command and fail[0]:
            raise subprocess.CalledProcessError(1, command)
        return SimpleNamespace(stdout='0.19.1\n', returncode=0)
    module=bootstrap()
    identity={'executable':'/fixture/python','version':'3.11'}
    with pytest.raises(subprocess.CalledProcessError):
        module.prepare(tmp_path, runner=run, identity=identity)
    assert not (tmp_path/'.setup/tools-venv.json').exists()
    fail[0]=False
    python=module.prepare(tmp_path, runner=run, identity=identity)
    assert python == tmp_path/'.setup/tools-venv/bin/python'
    install=next(c for c in calls if 'install' in c)
    assert '--require-hashes' in install and '--only-binary=:all:' in install
    calls.clear()
    module.prepare(tmp_path,runner=run,identity=identity)
    assert not any('install' in c or 'venv' in c for c in calls)
    lock.write_text('new-lock')
    calls.clear()
    module.prepare(tmp_path,runner=run,identity=identity)
    assert any('install' in c for c in calls)
    calls.clear()
    module.prepare(tmp_path,runner=run,identity={**identity,'version':'3.12'})
    assert any('venv' in c for c in calls)


def test_bootstrap_will_not_delete_an_unowned_environment(tmp_path):
    (tmp_path/'requirements').mkdir()
    (tmp_path/'requirements/setup.lock').write_text('lock')
    env=tmp_path/'.setup/tools-venv'
    env.mkdir(parents=True)
    value=env/'private-file';value.write_text('keep')
    with pytest.raises(ValueError, match='owned'):
        bootstrap().prepare(tmp_path, runner=lambda *a,**kw: None)
    assert value.read_text() == 'keep'
