"""Qwen test files shared by native UDS tests and real Compose containers."""
from project_templates import template_path, copy_templates
import json
from pathlib import Path
import shutil
import time

ROOT = Path(__file__).resolve().parents[1]
IDENTITIES = ('qwen35_single', 'qwen35_twostage')


def prepare_policies(root):
    root = Path(root)
    for identity, workflow, value in zip(IDENTITIES, ('single', 'two_stage'), (11, 22)):
        directory = root / identity
        checkpoint = directory / 'weights'
        checkpoint.mkdir(parents=True, exist_ok=True)
        manifest = json.loads((template_path(ROOT / 'policy' / identity / 'policy.json')).read_text())
        manifest.pop('deployment', None)  # CPU test backend has no GPU/container settings
        manifest['params']['model_config_path'] = str(directory / 'model.json')
        manifest['tui_commands']['probe'] = {'handler': 'probe', 'args': []}
        (directory / 'policy.json').write_text(json.dumps(manifest))
        entry = (ROOT / 'policy' / identity / 'main.py').read_text()
        # Test-only substitution after loading the unchanged production entry.
        entry += '''
from .test_backend import make_policy as AsyncQwenPolicy
_original_policy = policy
def policy(context, state, action_chunk, images, config, *, runtime):
    assert all(not image.flags.writeable for image in images.values())
    return _original_policy(context, state, action_chunk, images, config, runtime=runtime)
def probe(context):
    return context.backend.describe()
'''
        (directory / 'main.py').write_text(entry)
        (directory / 'main.py').with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
        shutil.copy2(ROOT / 'tests/fixtures/qwen_test_backend.py', directory / 'test_backend.py')
        (checkpoint / 'config.json').write_text(json.dumps({'type': 'qwen3_5vla_ki_wan', 'chunk_size': 50}))
        values = {'min': [-1] * 14, 'max': [1] * 14, 'mean': [0] * 14, 'std': [1] * 14}
        (checkpoint / 'stats.json').write_text(json.dumps({'arx_acone': {'observation.state': values, 'action': values}}))
        (checkpoint / 'model.safetensors').write_bytes(b'test-only model boundary')
        (directory / 'model.json').write_text(json.dumps({
            'model': {'checkpoint': 'weights', 'workflow': workflow},
            'runtime': {'instruction': 'test only'}, 'execution': {'inference_timeout_s': 5}}))
        (directory / 'controls.json').write_text(json.dumps({'value': value}))
        (directory / 'events.jsonl').write_text('')
    chain = root / 'qwen_test_chain'
    chain.mkdir(exist_ok=True)
    (chain / 'policy.json').write_text(json.dumps({
        'schema_version': 3, 'id': 'qwen_test_chain', 'kind': 'chain', 'policies': list(IDENTITIES)}))
    # Compose-stop fixture initializes both independent models but stays paused:
    # it exercises chain lifecycle without inventing dual-generator inference.
    rc = json.loads((template_path(ROOT / 'configs/rc.mock.json')).read_text())
    rc.update(policy_directory=str(root), startup_policy='qwen_test_chain',
              outputs_directory='/tmp/qwen-compose-stop-outputs')
    (root / 'rc.json').write_text(json.dumps(rc))
    return root


def set_controls(directory, **values):
    path = Path(directory) / 'controls.json'
    current = json.loads(path.read_text()) if path.exists() else {}
    current.update(values)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(current))
    temporary.replace(path)


def events(directory, phase=None):
    path = Path(directory) / 'events.jsonl'
    if not path.exists():
        return []
    # A reader may see the final line while the other process writes it.
    rows = [json.loads(line) for line in path.read_text().splitlines(keepends=True) if line.endswith('\n')]
    return [row for row in rows if phase is None or row['phase'] == phase]


def wait(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError('Qwen integration condition timed out')


if __name__ == '__main__':
    import sys
    prepare_policies(sys.argv[1])
