"""Plain table output must never execute terminal controls or import the UI."""
import json
import os
import subprocess
import sys
from pathlib import Path

from robot_control.cli import format_event


def test_table_headless_is_literal_and_retains_copy_json():
    raw = 'A\tB\r\n前\x1b[31m\x00\r尾'
    event = {'message': raw, 'table_document': {'schema_version': 1}, 'copy_json': json.dumps(raw)}
    result = format_event(event)
    assert result.endswith('A   B\n前�[31m�\n尾')
    assert '\x1b' not in result and '\r' not in result and '\x00' not in result
    assert json.loads(event['copy_json']) == raw


def test_cli_table_cleaner_has_no_rich_textual_import():
    script = '''import sys
from robot_control.cli import format_event
print(format_event({'message':'x\\x1b[31m', 'table_document':{}}))
assert 'rich' not in sys.modules and 'textual' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-c', script], text=True, capture_output=True, env={**os.environ, 'PYTHONPATH': os.pathsep.join(str(Path(__file__).resolve().parents[1] / part) for part in ('src', 'packages/policy-sdk/src'))})
    assert result.returncode == 0, result.stderr
    assert '\x1b' not in result.stdout
