"""Every model/LLM policy ships its own business sources and build inputs."""
import ast
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from robot_control.yaml_config import load_yaml_config

ROOT = Path(__file__).resolve().parents[1]
MODULES = {
    'qwen35_single': 'rc_qwen35_policy',
    'qwen35_twostage': 'rc_qwen35_twostage',
    'qwen35_history': 'rc_qwen35_history',
    'qwen35_history_lift2': 'rc_qwen35_history_lift2',
    'pi05': 'rc_pi05_policy',
    'internvla_maas': 'rc_internvla_maas',
    'llm_direction': 'rc_llm_direction',
    'llm_failure': 'rc_llm_failure',
}


def test_only_public_sdks_remain_in_packages():
    assert {p.name for p in (ROOT/'packages').iterdir() if p.is_dir()} == {'policy-sdk','connector-sdk'}
    assert not (ROOT/'vendor/qwen35').exists()
    assert not (ROOT/'vendor/qwen35_history').exists()


@pytest.mark.parametrize('identity,module', MODULES.items())
def test_policy_owns_all_declared_sources_and_dependency_build_inputs(identity, module):
    folder = ROOT/'policy'/identity
    assert (folder/'src'/module/'__init__.py').is_file()
    assert (folder/'pyproject.toml').is_file()
    service = load_yaml_config(folder/'compose.yaml')['services']['policy']
    prefix = '/workspace/policy/'+identity
    origins = json.loads(service['environment']['RC_SOURCE_MODULES'])
    assert origins[module] == prefix+'/src'
    assert all(value.startswith(prefix+'/') for value in origins.values())
    assert all(value.startswith(prefix+'/') for value in service['environment']['PYTHONPATH'].split(':'))
    for mount in service.get('volumes', []):
        source = mount['source']
        if source.startswith('${RC_HOST_PROJECT_ROOT'):
            assert source.split('}',1)[1].startswith('/policy/'+identity+'/'),source
            assert mount['read_only'] is True
    build = service['build']
    assert (folder/build['context']/build['dockerfile']).resolve() == folder/'Dockerfile'
    for value in build.get('additional_contexts', {}).values():
        assert (folder/value).resolve().is_relative_to(folder)
    if identity.startswith('qwen35_'):
        assert origins['lerobot'] == prefix+'/vendor'
        assert (folder/'vendor/lerobot/__init__.py').is_file()
    for path in (folder/'src').rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            names = ([node.module] if isinstance(node,ast.ImportFrom) and node.level==0 and node.module
                     else [name.name for name in node.names] if isinstance(node,ast.Import) else [])
            for name in names:
                if name.startswith('rc_'):
                    assert name.split('.')[0] in (module,'rc_policy_sdk'),(path,name)


@pytest.mark.parametrize('identity,module', MODULES.items())
def test_entry_imports_with_only_its_own_source_tree_and_public_sdk(tmp_path, identity, module):
    source = ROOT/'policy'/identity
    assert (source/'src'/module).is_dir()
    folder = tmp_path/identity
    shutil.copytree(source/'src',folder/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copyfile(source/'main.py',folder/'main.py')
    script = '''
import importlib, importlib.util, pathlib, sys
folder=pathlib.Path(sys.argv[1])
module_name=sys.argv[2]
spec=importlib.util.spec_from_file_location('isolated_entry',folder/'main.py')
entry=importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)
module=importlib.import_module(module_name)
assert pathlib.Path(module.__file__).is_relative_to(folder/'src')
assert callable(entry.initialize) and callable(entry.policy)
assert not ({'torch','transformers','lerobot','rclpy','rospy'} & sys.modules.keys())
for name,loaded in tuple(sys.modules.items()):
    if name.startswith('rc_') and not name.startswith('rc_policy_sdk'):
        assert name.split('.')[0]==module_name,name
        assert pathlib.Path(loaded.__file__).is_relative_to(folder/'src'),loaded.__file__
'''
    env = dict(os.environ,PYTHONPATH=os.pathsep.join([str(folder/'src'),str(ROOT/'packages/policy-sdk/src')]))
    result = subprocess.run([sys.executable,'-c',script,str(folder),module],cwd=tmp_path,env=env,
                            text=True,capture_output=True)
    assert result.returncode==0,result.stderr


def test_copied_variants_have_distinct_modules_and_local_provider_helpers(monkeypatch):
    for identity in MODULES:
        monkeypatch.syspath_prepend(str(ROOT/'policy'/identity/'src'))
    from rc_qwen35_policy.runtime import AsyncQwenPolicy
    from rc_qwen35_twostage.runtime import AsyncQwenPolicy as TwoStagePolicy
    from rc_qwen35_history.runtime import HistoryPolicy
    from rc_qwen35_history_lift2.runtime import HistoryPolicy as Lift2Policy
    from rc_llm_direction.provider_config import load_provider_config as direction_reader
    from rc_llm_failure.provider_config import load_provider_config as failure_reader
    assert AsyncQwenPolicy is not TwoStagePolicy and HistoryPolicy is not Lift2Policy
    assert direction_reader is not failure_reader


@pytest.mark.parametrize('identity', ['qwen35_single', 'qwen35_twostage',
                                     'qwen35_history', 'qwen35_history_lift2'])
def test_owned_vendor_provenance_references_local_files(identity):
    vendor = ROOT / 'policy' / identity / 'vendor'
    manifest = json.loads((vendor / 'SOURCE_MANIFEST.json').read_text())
    records = manifest['files']
    files = records.keys() if isinstance(records, dict) else (record['file'] for record in records)
    for file in files:
        assert (vendor / file).is_file()
    for record in manifest.get('derived_files', []):
        path = ROOT / record['file']
        assert path.is_file() and path.is_relative_to(vendor.parent)


@pytest.mark.parametrize('identities', [('qwen35_single', 'qwen35_twostage'),
                                      ('qwen35_history', 'qwen35_history_lift2')])
def test_independent_build_inputs_can_still_share_the_dependency_image(monkeypatch, identities):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    from setup_project import build_fingerprint
    fingerprints = []
    images = []
    for identity in identities:
        folder = ROOT / 'policy' / identity
        service = load_yaml_config(folder / 'compose.yaml')['services']['policy']
        build = service['build']
        build['context'] = str((folder / build['context']).resolve())
        build['additional_contexts'] = {
            name: str((folder / path).resolve())
            for name, path in build['additional_contexts'].items()
        }
        images.append(service['image'])
        fingerprints.append(build_fingerprint(build))
    assert images[0] == images[1]
    assert fingerprints[0] == fingerprints[1]


def test_policy_specific_scripts_and_documentation_are_owned():
    assert not (ROOT/'scripts/install_qwen35_patches.py').exists()
    assert not (ROOT/'scripts/migrate_llm_failure_prompts.py').exists()
    for identity in ('qwen35_single','qwen35_twostage'):
        assert (ROOT/'policy'/identity/'scripts/check_patches.py').is_file()
        assert (ROOT/'policy'/identity/'README.md').is_file()
    assert (ROOT/'policy/llm_failure/scripts/migrate_prompts.py').is_file()
    for name in ('pi05','internvla_maas','llm_direction','llm_failure','qwen35_history','qwen35_history_lift2'):
        assert (ROOT/'policy'/name/'README.md').is_file()


@pytest.mark.parametrize('identity,module', MODULES.items())
def test_scope_propagates_policy_owned_sources_to_worker_processes(monkeypatch, identity, module):
    spec=importlib.util.spec_from_file_location('local_layout_test_scope',ROOT/'scripts/test_scope.py')
    scope=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scope)
    calls=[]
    monkeypatch.setattr(sys,'argv',['test_scope.py','--policy',identity,'--collect-only'])
    monkeypatch.setattr(scope.os,'execve',lambda executable,args,env: calls.append(env))
    scope.main()
    assert str(ROOT/'policy'/identity/'src') in calls[0]['PYTHONPATH'].split(os.pathsep)
