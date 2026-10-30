"""Source mounts are the only application code delivery path."""
import importlib
import json
from pathlib import Path

import pytest

from test_setup_dynamic import ROOT, read_compose


@pytest.mark.parametrize('path', [ROOT / 'docker/rc.Dockerfile', ROOT / 'docker/ipc-anchor.Dockerfile',
                                *sorted((ROOT / 'policy').glob('*/Dockerfile'))])
def test_images_do_not_copy_or_build_repository_python(path):
    text = path.read_text()
    for line in text.splitlines():
        if line.startswith('COPY '):
            assert not any(source in line for source in ('COPY src ', 'COPY packages/', 'COPY vendor/',
                'COPY tests', 'COPY scripts', 'COPY docker/', 'COPY policy policy', 'COPY configs ', 'COPY pyproject.toml'))
            if line.startswith('COPY policy/'):
                assert '.lock ' in line, line
    assert '/src/policy-sdk' not in text and 'pip wheel --no-deps' not in text
    assert 'install_qwen35_patches.py' not in text


def test_base_and_simulator_mount_source_readonly():
    services = read_compose(ROOT / 'compose.yaml')['services']
    assert set(services) == {'ipc-anchor', 'robot-control'}
    # Base compose only wires the RC service to the IPC anchor, which must be healthy first.
    assert set(services['robot-control']['depends_on']) == {'ipc-anchor'}
    assert services['robot-control']['depends_on']['ipc-anchor']['condition'] == 'service_healthy'
    mounts = {mount['target']: mount for mount in services['robot-control']['volumes']}
    for target in ('/workspace/src', '/workspace/packages', '/workspace/docker'):
        assert mounts[target]['read_only'] and not mounts[target]['bind'].get('create_host_path', False)
    anchor = {m['target']: m for m in services['ipc-anchor']['volumes']}
    assert anchor['/workspace/docker']['read_only']
    assert '/workspace/src' in services['robot-control']['environment']['PYTHONPATH']
    assert services['robot-control']['environment']['PYTHONPYCACHEPREFIX'].startswith('/tmp/')
    sim = read_compose(ROOT / 'compose.yaml', ROOT / 'compose.ros-sim.yaml')['services']['ros-sim']
    assert any(m['target'] == '/workspace/ros_ws/src/robot_control_test_sim' and m['read_only'] for m in sim['volumes'])


@pytest.mark.parametrize('identity,module', [('qwen35_single','rc_qwen35_policy'), ('qwen35_twostage','rc_qwen35_policy'),
    ('llm_direction','rc_llm_direction'), ('llm_failure','rc_llm_failure'), ('pi05','rc_pi05_policy')])
def test_policy_declares_own_source_paths(identity, module):
    service = read_compose(ROOT / 'policy' / identity / 'compose.yaml')['services']['policy']
    origins = json.loads(service['environment']['RC_SOURCE_MODULES'])
    mounts = {m['target']: m for m in service['volumes']}
    assert module in origins
    assert mounts[origins[module]]['read_only']
    assert origins[module] in service['environment']['PYTHONPATH'].split(':')
    if identity.startswith('qwen35_'):
        assert origins['lerobot'] == '/workspace/vendor/qwen35'
        patch = '/usr/local/lib/python3.11/site-packages/transformers/models/qwen3_5/modeling_qwen3_5.py'
        assert mounts[patch]['read_only'] and not mounts[patch]['bind'].get('create_host_path', False)
    else:
        assert 'lerobot' not in origins
        assert '/workspace/vendor/qwen35' not in service['environment']['PYTHONPATH']


def test_source_validation_rejects_missing_or_shadowed_module(tmp_path, monkeypatch):
    from rc_policy_sdk.source import validate_sources
    first, shadow = tmp_path / 'first', tmp_path / 'shadow'
    for folder in (first, shadow):
        package = folder / 'source_test_package'
        package.mkdir(parents=True)
        (package / '__init__.py').write_text('')
    monkeypatch.syspath_prepend(str(first))
    validate_sources({'source_test_package': str(first)})
    monkeypatch.syspath_prepend(str(shadow))
    importlib.invalidate_caches()
    with pytest.raises(RuntimeError, match='source'):
        validate_sources({'source_test_package': str(first)})
    with pytest.raises(RuntimeError, match='source'):
        validate_sources({'not_a_package': str(tmp_path / 'missing')})


def test_manager_injects_sdk_and_keeps_custom_pythonpath(tmp_path):
    from test_runtime_manager import manager
    from robot_control.config import PolicyRecord
    import time
    runtime, _ = manager(tmp_path)
    folder = tmp_path / 'policy/custom'
    folder.mkdir(parents=True)
    (folder / 'main.py').write_text('')
    (folder / 'compose.yaml').write_text(json.dumps({'services': {'policy': {'image':'fixture:local',
        'environment': {'PYTHONPATH':'/custom'}}}}))
    spec = runtime.prepare_spec(PolicyRecord(folder, {'id':'custom'}), deadline=time.monotonic()+3)
    service = json.loads(spec.model_json)['services']['policy']
    sdk = '/workspace/packages/policy-sdk/src'
    mounts = {m['target']:m for m in service['volumes']}
    assert mounts[sdk]['read_only'] and not mounts[sdk]['bind'].get('create_host_path', False)
    assert service['environment']['PYTHONPATH'] == sdk + ':/custom'
    assert json.loads(service['environment']['RC_SOURCE_MODULES'])['rc_policy_sdk'] == sdk
    assert service['entrypoint'] == ['python', sdk + '/rc_policy_sdk/source.py', '--module', 'rc_policy_sdk.host']


@pytest.mark.parametrize('source_root,mounts', [
    ('/custom/src', [('/custom', True), ('/custom/src', False)]),
    ('/custom/src', [('/custom/src', True), ('/custom/src/my_policy/main.py', False)]),
    ('/custom/../writable', [('/custom', True), ('/writable', False)]),
    ('/custom/src', [('/custom/src', True), ('/custom/src/../src/my_policy/main.py', False)]),
])
def test_manager_rejects_writable_source_overrides_and_path_traversal(tmp_path, source_root, mounts):
    from test_runtime_manager import manager
    from robot_control.config import PolicyRecord
    import time
    runtime, _ = manager(tmp_path)
    folder = tmp_path / 'policy/custom'
    folder.mkdir(parents=True)
    service = {'image': 'fixture:local',
               'environment': {'RC_SOURCE_MODULES': json.dumps({'my_policy':source_root})},
               'volumes': [dict(type='bind',source=str(tmp_path/'source'),target=target,read_only=readonly)
                           for target,readonly in mounts]}
    (folder/'compose.yaml').write_text(json.dumps({'services':{'policy':service}}))
    with pytest.raises(ValueError, match='source|mount target'):
        runtime.prepare_spec(PolicyRecord(folder, {'id':'custom'}), deadline=time.monotonic()+3)


@pytest.mark.parametrize('target', ['/workspace/packages/policy-sdk/src/rc_policy_sdk/runtime.py',
                                  '/workspace/policy/custom/main.py'])
def test_manager_rejects_writable_overrides_of_injected_sources(tmp_path, target):
    from test_runtime_manager import manager
    from robot_control.config import PolicyRecord
    import time
    runtime, _ = manager(tmp_path)
    folder = tmp_path / 'policy/custom'
    folder.mkdir(parents=True)
    service = {'image': 'fixture:local', 'volumes': [dict(type='bind',source=str(tmp_path/'source'),
                                                       target=target,read_only=False)]}
    (folder/'compose.yaml').write_text(json.dumps({'services':{'policy':service}}))
    with pytest.raises(ValueError, match='source'):
        runtime.prepare_spec(PolicyRecord(folder, {'id':'custom'}), deadline=time.monotonic()+3)
