"""Source-only fixtures for isolated Docker deployments."""
import json
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def copy_runtime_sources(destination):
    # No configs, credentials, output files or host bytecode enter the fixture.
    for name in ('src', 'packages', 'docker', 'ros_ws/src/robot_control_test_sim'):
        shutil.copytree(ROOT / name, Path(destination) / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info', '.DS_Store'))
    copy_connectors(destination)


def copy_connectors(destination):
    from project_templates import copy_templates
    return copy_templates(ROOT / 'connector', Path(destination) / 'connector',
        ignore=lambda directory, names: {name for name in names if name != 'compose.yaml' and
                                         name.endswith(('.yaml', '.yml', '.json'))})


def configure_connector(config, directory, **values):
    from config_helpers import read_config
    config['connector_directory'] = '../connector'
    for spec in config['connectors'].values():
        spec['config'] = spec['config'].removesuffix('.example')
        path = Path(directory) / 'connector' / spec['source'] / spec['config']
        device = read_config(path)
        device.update(values)
        path.write_text(json.dumps(device))


def source_report():
    from rc_policy_sdk.source import validate_sources
    return validate_sources(json.loads(os.environ['RC_SOURCE_MODULES']))


def anchor_override(services):
    services['ipc-anchor'] = {'image': os.environ.get('RC_TEST_ANCHOR_IMAGE', 'robot-control-compose-anchor:local')}
