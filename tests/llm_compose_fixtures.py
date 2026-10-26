"""Prepare real policy entries with test-only HTTP configuration in a named volume."""
import json
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1])
root.mkdir(parents=True, exist_ok=True)
source = Path(__file__).resolve().parents[1]
manifest = json.loads((source / 'policy/llm_direction/policy.json').read_text())
manifest['params'].update(task='move up and stop', chunk_size=3)
manifest['params']['provider'].update(base_url='http://llm-model:8080/v1', timeout_s=10)
for name in ('llm_direction', 'B'):
    folder = root / name
    shutil.copytree(source / 'policy/llm_direction', folder, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__'))
    (folder / 'policy.json').write_text(json.dumps({**manifest, 'id': name}))
(root / 'B/main.py').write_text('''
def initialize(config): return None
def policy(context, state, action_chunk, images, config): return action_chunk
def reset_context(context): pass
def release(context): pass
''')
(root / 'default_chain').mkdir(exist_ok=True)
(root / 'default_chain/policy.json').write_text(json.dumps({
    'schema_version': 2, 'id': 'default_chain', 'kind': 'chain',
    'policies': ['llm_direction', 'B', 'llm_direction']}))
rc = json.loads((source / 'configs/rc.mock.json').read_text())
rc.update(policy_directory=str(root), startup_policy='llm_direction', outputs_directory='/tmp/llm-outputs')
(root / 'rc.json').write_text(json.dumps(rc))
