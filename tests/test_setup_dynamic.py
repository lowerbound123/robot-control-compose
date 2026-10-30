from config_helpers import read_config
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))


def test_setup_selection_is_catalog_not_installed_list(tmp_path):
    from setup_project import select_policies
    from test_policy_catalog_v3 import plugin
    plugin(tmp_path/'policy','never-seen-before')
    assert select_policies(tmp_path,['never-seen-before'])==['never-seen-before']
    assert select_policies(tmp_path,[])==[]


def test_build_identity_uses_dockerfile_contents_and_effective_args(tmp_path):
    from setup_project import build_fingerprint
    (tmp_path/'one').mkdir(); (tmp_path/'two').mkdir()
    for name in ['one','two']:
        (tmp_path/name/'Dockerfile').write_text('FROM busybox:1.37.0\n')
    a={'context':str(tmp_path),'dockerfile':'one/Dockerfile','args':{'VERSION':'1'},'target':'runtime'}
    b={**a,'dockerfile':'two/Dockerfile'}
    assert build_fingerprint(a)==build_fingerprint(b)
    assert build_fingerprint(a)!=build_fingerprint({**b,'args':{'VERSION':'2'}})


def test_qwen_deployments_are_self_contained():
    for identity in ('qwen35_single','qwen35_twostage'):
        directory=ROOT/'policy'/identity
        model=read_compose(directory/'compose.yaml')
        assert set(model['services'])=={'policy'}
        assert (directory/'Dockerfile').is_file()
        assert (directory/'dependencies/requirements.lock').is_file()
        assert 'policy_host' not in read_config(directory/'policy.yaml.example')


def read_compose(path, *overlays):
    import os,subprocess
    if not shutil.which('docker'):
        pytest.skip('Docker CLI required for Compose config validation')
    env=dict(os.environ,RC_HOST_PROJECT_ROOT=str(ROOT),RC_BASE_PROJECT='fixture',RC_UID='1000',RC_GID='1000',RC_DOCKER_GID='0',
             RC_DOCKER_SOCKET='/var/run/docker.sock',RC_RPC_VOLUME='fixture-rpc',MODELS_DIR='/models',HF_CACHE_DIR='/cache')
    files = [arg for file in (path, *overlays) for arg in ('-f', str(file))]
    return json.loads(subprocess.check_output(['docker','compose','--env-file','/dev/null', *files,
        'config','--format','json','--no-path-resolution'],env=env))
