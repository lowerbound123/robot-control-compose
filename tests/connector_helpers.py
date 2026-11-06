"""Load device implementations for adapter unit tests, never through RC."""
import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / 'connector').is_dir() and Path('/workspace/connector').is_dir(): ROOT = Path('/workspace')
sys.path.insert(0, str(ROOT / 'packages/connector-sdk/src'))


def source(name):
    spec = importlib.util.spec_from_file_location('test_connector_' + name, ROOT / 'connector' / name / 'main.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mock = source('mock')
acone = source('acone')
MockRobot, ACOneRobot = mock.MockRobot, acone.ACOneRobot
