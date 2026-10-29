"""Exercise the actual catalog and Compose declaration without a model/GPU."""
from config_helpers import read_config
import json
from pathlib import Path
import shutil
import sys

from robot_control.policy_catalog import discover
from test_setup_dynamic import read_compose

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


def test_pi05_is_discovered_without_an_rc_registry_and_has_its_own_image(tmp_path):
    from setup_project import select_policies

    directory = tmp_path / 'policy/pi05'
    directory.mkdir(parents=True)
    source = ROOT / 'policy/pi05'
    for name in ('main.py', 'compose.yaml'):
        shutil.copyfile(source / name, directory / name)
    shutil.copyfile(source / 'policy.yaml.example', directory / 'policy.yaml')
    assert discover(tmp_path / 'policy')[0]['valid'] is True
    assert select_policies(tmp_path, ['pi05']) == ['pi05']
    model = read_compose(source / 'compose.yaml')
    assert set(model['services']) == {'policy'}
    service = model['services']['policy']
    assert service['image'] == 'robot-control-compose-pi05:local'
    assert service['platform'] == 'linux/amd64'
    assert service['environment']['HF_HUB_OFFLINE'] == '1'
    assert service['environment']['USER'] == service['environment']['LOGNAME'] == 'robot-control'
    assert not any(m['target'] == '/var/run/docker.sock' for m in service['volumes'])
    assert service['deploy']['resources']['reservations']['devices'][0]['capabilities'] == ['gpu']
    config = read_config(directory / 'policy.yaml')
    assert config['tui_commands']['switch-model']['effect'] == 'reload_policy'
