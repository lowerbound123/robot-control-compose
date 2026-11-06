"""Read shipped/test main YAML without changing business JSON parsing semantics."""
import json
from pathlib import Path


def read_config(path):
    path = Path(path)
    if path.name.endswith(('.yaml', '.yml', '.yaml.example', '.yml.example')):
        from robot_control.yaml_config import load_yaml_config
        value = load_yaml_config(path)
        # Shipped RC examples are exercised directly by host tests, without
        # creating private configuration in the developer's checkout.
        if path.name.endswith('.example') and path.name.startswith('rc') and 'connectors' in value:
            root = Path(__file__).resolve().parents[1]
            value['connector_directory'] = str(root / 'connector')
            for spec in value['connectors'].values(): spec['config'] += '.example'
        return value
    return json.loads(path.read_text(encoding='utf-8'))


def connector_config(config, *, source='mock', device=None):
    """Explicit fixture migration to the new RC configuration, not a runtime fallback."""
    root = Path(__file__).resolve().parents[1]
    config['connector_directory'] = str(root / 'connector')
    config['connectors'] = {'body': {'source': source, 'config': str(device) if device else 'devices/default.yaml.example'}}
    config['robot'] = {'connector': 'body'}
    return config
