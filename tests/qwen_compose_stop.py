"""Check real Compose stop with both Qwen contexts loaded in a paused chain."""
import json
from pathlib import Path
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
PROJECT = 'rc-qwen-stop-' + uuid.uuid4().hex[:8]
BASE = ['docker', 'compose', '-p', PROJECT, '-f', 'compose.yaml',
        '-f', 'compose.qwen.yaml', '-f', 'compose.qwen-test.yaml']


def compose(*args):
    return subprocess.check_output(BASE + list(args), cwd=ROOT, text=True)


def rows():
    script = '''
import json
from pathlib import Path
print(json.dumps({name:[json.loads(line)['phase'] for line in (Path('/workspace/policy')/name/'events.jsonl').read_text().splitlines()] for name in ('qwen35_single','qwen35_twostage')}))
'''
    return json.loads(compose('exec', '-T', 'policy-qwen35-single', 'python', '-c', script))


try:
    compose('up', '-d', '--no-build', 'robot-control')
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if (all('warmup' in phases for phases in rows().values()) and
                'policy qwen_test_chain ready' in compose('logs', '--no-color', 'robot-control')):
            break
        time.sleep(.2)
    else:
        raise AssertionError('both Qwen contexts did not initialize')
    ids = {name: compose('ps', '-q', name).strip() for name in
           ('robot-control', 'policy-qwen35-single', 'policy-qwen35-twostage')}
    info = {name: json.loads(subprocess.check_output(['docker', 'inspect', identity], text=True))[0]
            for name, identity in ids.items()}
    assert info['policy-qwen35-single']['Image'] == info['policy-qwen35-twostage']['Image']
    compose('stop')
    for name, identity in ids.items():
        status = json.loads(subprocess.check_output(['docker', 'inspect', identity], text=True))[0]['State']
        assert not status['Running'] and status['ExitCode'] == 0, (name, status)
    # A standalone read-only volume probe does not join the stopped IPC anchor.
    volume = next(m['Name'] for m in info['policy-qwen35-single']['Mounts'] if m['Destination'] == '/workspace/policy')
    probe = '''
import json
from pathlib import Path
for name in ('qwen35_single','qwen35_twostage'):
    phases=[json.loads(line)['phase'] for line in (Path('/probe')/name/'events.jsonl').read_text().splitlines()]
    assert phases.count('load') == phases.count('close') == 1, (name,phases)
    assert 'infer' not in phases, phases
print('Both Qwen models released once; paused chain never inferred: PASS')
'''
    print(subprocess.check_output(['docker', 'run', '--rm', '--entrypoint', 'python3', '-v', volume + ':/probe:ro',
                                   'robot-control-compose-tests:local', '-c', probe], text=True).strip())
    print('Same image / independent containers / Compose SIGTERM clean exit: PASS')
except BaseException:
    print(compose('logs', '--no-color', '--tail=100'))
    raise
finally:
    compose('down', '-v')
