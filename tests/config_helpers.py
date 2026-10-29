"""Read shipped/test main YAML without changing business JSON parsing semantics."""
import json
from pathlib import Path


def read_config(path):
    path = Path(path)
    if path.name.endswith(('.yaml', '.yml', '.yaml.example', '.yml.example')):
        from robot_control.yaml_config import load_yaml_config
        return load_yaml_config(path)
    return json.loads(path.read_text(encoding='utf-8'))
