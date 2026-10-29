"""Source-only fixtures for isolated Docker deployments."""
import json
import os
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]


def copy_runtime_sources(destination):
    # No configs, credentials, output files or host bytecode enter the fixture.
    for name in ('src', 'packages', 'docker', 'vendor/qwen35', 'ros_ws/src/robot_control_test_sim'):
        shutil.copytree(ROOT / name, Path(destination) / name,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.egg-info', '.DS_Store'))


def source_report():
    from rc_policy_sdk.source import validate_sources
    return validate_sources(json.loads(os.environ['RC_SOURCE_MODULES']))


def anchor_override(services):
    services['ipc-anchor'] = {'image': os.environ.get('RC_TEST_ANCHOR_IMAGE', 'robot-control-compose-anchor:local')}
