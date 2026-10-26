"""Start a real slow HTTP inference, then stop the isolated Compose stack."""
import json
from pathlib import Path
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
PROJECT = 'rc-llm-stop-' + uuid.uuid4().hex[:8]
BASE = ['docker', 'compose', '-p', PROJECT, '-f', 'compose.yaml',
        '-f', 'compose.llm.yaml', '-f', 'compose.llm-test.yaml']


def compose(*args):
    return subprocess.check_output(BASE + list(args), cwd=ROOT, text=True)


def model(path, value=None):
    code = '''
import json, urllib.request
request=urllib.request.Request('http://127.0.0.1:8080'+PATH,
 data=None if VALUE is None else json.dumps(VALUE).encode(),headers={'Content-Type':'application/json'})
with urllib.request.urlopen(request,timeout=3) as response: print(response.read().decode())
'''.replace('PATH', repr(path)).replace('VALUE', repr(value))
    return json.loads(compose('exec', '-T', 'llm-model', 'python3', '-c', code))


try:
    compose('up', '-d', '--no-build', 'robot-control')
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if 'policy llm_direction ready' in compose('logs', '--no-color', 'robot-control'):
            break
        time.sleep(.2)
    else:
        raise AssertionError('controller did not load policy')
    ids = {name: compose('ps', '-q', name).strip() for name in ('robot-control', 'policy-llm-direction')}
    model('/control', {'responses': [{'delay': 3}]})
    # Use the production MCP takeover/return path to resume, entirely inside the test container.
    code = '''
import asyncio
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
async def main():
    async with httpx2.AsyncClient(timeout=15) as http:
        async with streamable_http_client('http://127.0.0.1:4000/mcp',http_client=http) as streams:
            async with ClientSession(streams[0],streams[1]) as session:
                await session.initialize()
                assert not (await session.call_tool('take',{})).is_error
                assert not (await session.call_tool('return',{'wait':True})).is_error
asyncio.run(main())
'''
    compose('exec', '-T', 'robot-control', 'python3', '-c', code)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if model('/requests'):
            break
        time.sleep(.1)
    else:
        raise AssertionError('slow request did not start')
    started = time.monotonic()
    compose('stop')
    assert time.monotonic() - started < 90
    for name, identity in ids.items():
        state = json.loads(subprocess.check_output(['docker', 'inspect', identity], text=True))[0]['State']
        assert not state['Running'] and state['ExitCode'] == 0, (name, state)
    print('Compose stop during HTTP inference: controller and host exited 0; PASS')
except BaseException:
    print(compose('logs', '--no-color', '--tail=80'))
    raise
finally:
    compose('down', '-v')
