"""Bootstrap validation aligned with RC/SDK contracts, using only the stdlib."""
import importlib.util
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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


def validate_rc(config):
    if config.get('schema_version') != 3:
        raise ValueError('RC schema_version does not match this repository')
    load('src/robot_control/connector_config.py').validate_connectors(config)
    positive(config.get('frequency_hz', 30), 'frequency_hz')
    vector(config.get('reset_qpos'), 'reset_qpos')
    load('src/robot_control/safety_config.py').safety_settings(config.get('safety', {}))
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
    load('src/robot_control/global_state_config.py').settings(config.get('global_state'))


def validate_policy(config, identity=None):
    load('packages/policy-sdk/src/rc_policy_sdk/catalog.py').validate_policy(config, identity)


def validate_documents(documents):
    for path, value in documents.items():
        location = Path(path)
        try:
            if (location.suffix in ('.yaml', '.yml') and location.parent in (Path('.'), Path('configs')) and
                    (location.stem == 'rc' or location.name.startswith('rc.'))):
                validate_rc(value)
            elif len(location.parts) == 3 and location.parts[0] == 'policy' and location.name == 'policy.yaml':
                validate_policy(value, location.parent.name)
            elif location.parent == Path('configs/robots') and location.suffix == '.yaml':
                load('src/robot_control/connector_config.py').validate_connector_profile(value)
            elif (location.parts[0] == 'connector' and 'devices' in location.parts and
                    location.suffix in ('.yaml', '.yml')):
                load('src/robot_control/safety_config.py').safety_settings(value.get('safety', {}))
        except (ValueError, TypeError, KeyError):
            raise ValueError(f'{path}: invalid main configuration schema') from None
