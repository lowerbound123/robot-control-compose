"""Exercise Git's actual source/config boundary in an isolated repository."""
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def ignored_paths(tmp_path):
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True, env=env)
    (tmp_path / '.gitignore').write_bytes((ROOT / '.gitignore').read_bytes())

    def check(paths):
        result = subprocess.run(
            ['git', '-c', 'core.excludesFile='+os.devnull, 'check-ignore', '--no-index', '-z', '--stdin'],
            cwd=tmp_path, env=env, input='\0'.join(paths)+'\0', capture_output=True, text=True)
        assert result.returncode in (0, 1), result.stderr
        return set(filter(None, result.stdout.split('\0')))
    return check


def test_new_source_names_do_not_need_individual_exceptions(ignored_paths):
    paths = ['compose.new_robot.yaml', 'compose.new_camera.yml', 'compose.yml']
    for kind in ('policy', 'connector'):
        root = kind+'/new_component'
        paths.extend([root+'/compose.yaml', root+'/compose.cpu.yml', root+'/pyproject.toml'])
        paths.extend(root+'/'+name for name in (
            'src/runtime/schemas/action.json', 'src/runtime/defaults.yaml',
            'vendor/SOURCE_MANIFEST.json', 'vendor/pyproject.toml',
            'vendor/new_library/schemas/robot.yml', 'vendor/new_library/configs/types.yaml',
            'vendor/new_library/models/config.json', 'dependencies/patches/sha256.json',
            'dependencies/compiler/config.toml'))
    assert ignored_paths(paths) == set()


def test_local_configuration_remains_private(ignored_paths):
    paths = ['rc.yaml', 'settings.json', 'configs/rc.new_robot.yaml',
             'configs/robots/new_robot.yaml', 'webui/config.json',
             'compose.local.yaml', 'compose.new_robot.local.yml', 'compose.override.yaml',
             'compose.override.yml', '.env', '.env.development', 'global.env']
    for kind in ('policy', 'connector'):
        root = kind+'/new_component'
        paths.extend(root+'/'+name for name in (
            'policy.yaml', 'model.json', 'provider.yaml', 'providers/private.yaml',
            'devices/private.yaml', 'deployment.env', 'compose.local.yaml',
            'compose.cpu.local.yml', 'compose.override.yaml', 'settings.ini',
            'config/session.conf', 'config/local.cfg'))
        for folder in ('src', 'vendor', 'dependencies'):
            paths.extend(root+'/'+folder+'/'+name for name in (
                '.env', '.env.development', 'deployment.env', 'settings.local.json'))
    assert ignored_paths(paths) == set(paths)


def test_source_exceptions_do_not_reinclude_runtime_artifacts(ignored_paths):
    paths = ['.codex/task-state.md', '.setup/backups/config.yaml', 'models/model.safetensors',
             'cache/huggingface/config.json', 'outputs/record/info.json',
             'ros_ws/install/setup.bash', 'webui/playwright-report/index.html',
             'webui/test-results/result.json']
    for kind in ('policy', 'connector'):
        root = kind+'/new_component'
        for folder in ('src', 'vendor', 'dependencies'):
            paths.extend(root+'/'+folder+'/'+name for name in (
                '__pycache__/module.pyc', '.pytest_cache/v/cache/nodeids',
                '.venv/pyvenv.cfg', 'build/config.json', 'dist/config.json',
                'outputs/record.json', 'snapshot/frame.png', '.runtime/state.json',
                'package.egg-info/metadata.json', 'node_modules/pkg/package.json'))
    assert ignored_paths(paths) == set(paths)


def test_configuration_templates_are_shipped_but_generated_files_are_ignored(ignored_paths):
    templates = [p.relative_to(ROOT).as_posix() for folder in ('configs', 'policy', 'connector')
                 for p in (ROOT/folder).rglob('*.example')]
    templates += ['.env.example', 'compose.new_robot.local.yaml.example']
    assert templates
    assert ignored_paths(templates) == set()
    generated = [p.removesuffix('.example') for p in templates]
    assert ignored_paths(generated) == set(generated)


def test_existing_tracked_sources_remain_visible_on_a_fresh_checkout(ignored_paths):
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT, text=True).split('\0')
    assert ignored_paths([p for p in tracked if p]) == set()
