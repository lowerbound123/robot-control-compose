"""Bootstrap validation aligned with RC/SDK contracts, using only the stdlib."""
import copy
import importlib.util
import math
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*')


def load(relative):
    path = ROOT / relative
    spec = importlib.util.spec_from_file_location('_setup_' + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def positive(value, field):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{field} must be positive and finite')


def vector(value, field):
    try:
        if not isinstance(value, list) or len(value) != 14:
            raise ValueError()
        numbers = [float(item) for item in value]
        if not all(math.isfinite(item) and abs(item) <= 3.4028234e38 for item in numbers):
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f'{field} must contain 14 finite numbers') from None
    return numbers


def validate_rc(config, mode):
    if config.get('schema_version') != 3:
        raise ValueError('RC schema_version does not match this repository')
    if not isinstance(config.get('robot'), dict) or config['robot'].get('backend') not in ('mock', 'acone'):
        raise ValueError('robot.backend must be mock or acone')
    positive(config.get('frequency_hz', 30), 'frequency_hz')
    vector(config.get('reset_qpos'), 'reset_qpos')
    safety = config.get('safety')
    if not isinstance(safety, dict):
        raise ValueError('safety must be an object')
    positive(safety.get('reference_frequency_hz', 50), 'safety.reference_frequency_hz')
    limits = vector(safety.get('max_delta_per_step', [-1] * 14), 'safety.max_delta_per_step')
    if any(item <= 0 and item != -1 for item in limits):
        raise ValueError('safety limits must be positive or -1')
    epos = safety.get('epos', {})
    if not isinstance(epos, dict) or set(epos) - {'translation_m_s', 'rotation_rad_s', 'gripper_units_s'}:
        raise ValueError('invalid safety.epos settings')
    for key, value in epos.items():
        positive(value, 'safety.epos.' + key)
    timeouts = config.get('timeouts', {})
    if not isinstance(timeouts, dict):
        raise ValueError('timeouts must be an object')
    for name, value in timeouts.items():
        positive(value, 'timeouts.' + name)
    for key in ('policy_directory', 'outputs_directory'):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ValueError(f'RC must specify {key}')
    if {'policy_hosts', 'conda_executable'} & config.keys():
        raise ValueError('legacy host/environment tables are not supported')
    load('src/robot_control/direct_config.py').control_settings(config.get('direct_control'))
    load('src/robot_control/mcp_config.py').mcp_settings(config.get('mcp'))
    load('src/robot_control/tui_config.py').tui_settings(config.get('tui'))


def validate_policy(config, mode, identity=None):
    load('packages/policy-sdk/src/rc_policy_sdk/catalog.py').validate_policy(config, identity)


def validate_documents(documents, mode='compose'):
    for path, value in documents.items():
        location = Path(path)
        if (location.parent in (Path('.'), Path('configs')) and
                (location.name == 'rc.json' or location.name.startswith('rc.') and location.suffix == '.json')):
            validate_rc(value, mode)
        elif path.endswith('/policy.json'):
            validate_policy(value, mode, Path(path).parent.name)
