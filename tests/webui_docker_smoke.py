"""Serve a disposable production-Compose WebUI + dynamic example deployment.

Writes connection/evidence metadata, then holds until SIGTERM. Cleanup uses the
same bounded owner-aware down path as deployment. Never touches another project.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'tests'), str(ROOT / 'src'), str(ROOT / 'packages/policy-sdk/src')]
from config_helpers import read_config
from project_templates import copy_templates
from setup_project import setup
from compose_project import compose_command, down
from source_helpers import anchor_override


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--evidence-dir', required=True, type=Path)
    parser.add_argument('--image', default='robot-control-compose-webui:local')
    args = parser.parse_args()
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='rc-webui-docker-'))
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    context = None
    command = None
    ready = False
    try:
        for name in ('src', 'packages/policy-sdk', 'docker'):
            shutil.copytree(ROOT / name, root / name, ignore=shutil.ignore_patterns('__pycache__', '*.egg-info'))
        for name in ('.env.example', 'compose.yaml', 'compose.webui.yaml'):
            shutil.copyfile(ROOT / name, root / name)
        (root / 'configs').mkdir()
        (root / 'webui').mkdir()
        copy_templates(ROOT / 'policy/example', root / 'policy/example')
        copy_templates(ROOT / 'policy/example_chain', root / 'policy/example_chain')
        config = read_config(ROOT / 'configs/rc.mock.yaml.example')
        config.update(startup_policy=None, frequency_hz=30,
                      timeouts=dict(initialize_s=30, inference_s=5, release_s=3, shutdown_s=25))
        config['robot']['image_size'] = [480, 640]
        (root / 'configs/rc.mock.yaml').write_text(json.dumps(config))
        setup(root, ['example_chain'], config_only=True)
        context = read_config(root / '.setup/runtime.json')
        (evidence / 'prepared.json').write_text(json.dumps({'root': str(root), 'project': context['base_project']}))
        gid = subprocess.check_output(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'stat',
            '--mount', f"type=bind,source={context['docker_socket']},target=/socket",
            'robot-control-compose-example:local', '-c', '%g', '/socket'], text=True, timeout=30).strip()
        context['docker_gid'] = int(gid)
        (root / '.setup/runtime.json').write_text(json.dumps(context))
        base_env = root / '.setup/base.env'
        base_env.write_text('\n'.join(f"RC_DOCKER_GID='{gid}'" if line.startswith('RC_DOCKER_GID=') else line
                                     for line in base_env.read_text().splitlines()) + '\n')
        with (root / '.env').open('a') as stream:
            stream.write('\nRC_MCP_PORT=0\nRC_WEBUI_PORT=0\nROBOT_CONTROL_MCP_TOKEN=webui-smoke-token\n')
        web_config = root / 'webui/config.json'
        # This file is mounted outside the web root and read only at startup.
        web_config.write_text(json.dumps({'endpoint': 'http://robot-control:18766/no-cors?literal=$host', 'model': 'local-fixture',
                                         'api_key': 'local-test-only', 'timeout_s': 3}))
        web_config.chmod(0o600)
        override = {'services': {
            'robot-control': {
                'image': os.environ.get('RC_TEST_RC_IMAGE', 'robot-control-compose-rc:local'),
                'command': ['python3', '/workspace/tests/webui_harness.py', '--config', '/workspace/configs/rc.mock.yaml',
                            '--host', '0.0.0.0', '--port', '4000', '--api-port', '18766'],
                'volumes': [{'type': 'bind', 'source': str(ROOT / 'tests'), 'target': '/workspace/tests', 'read_only': True}],
                'ports': ['127.0.0.1:0:18766'],
            },
            'webui': {'image': args.image},
        }}
        anchor_override(override['services'])
        (root / 'override.json').write_text(json.dumps(override))
        prefix = ['-f', 'compose.webui.yaml', '-f', 'override.json']
        command = compose_command(root, prefix)
        subprocess.run(command + ['up', '-d', '--no-build'], check=True, timeout=90)
        def container(service):
            return subprocess.check_output(command + ['ps', '-aq', service], text=True).strip()
        rc_id, web_id = container('robot-control'), container('webui')
        def port(cid, inner):
            value = json.loads(subprocess.check_output(['docker', 'inspect', cid], text=True))[0]
            return value['NetworkSettings']['Ports'][f'{inner}/tcp'][0]['HostPort']
        url = 'http://127.0.0.1:' + port(web_id, 8080)
        api = 'http://127.0.0.1:' + port(rc_id, 18766)
        deadline = time.monotonic() + 45
        while True:
            try:
                with urlopen(url + '/health', timeout=2) as response:
                    health = json.load(response)
                if health['state'] == 'paused': break
            except Exception:
                if time.monotonic() >= deadline: raise
            time.sleep(.1)
        for path in ('/monitor', '/config.json', '/preview/status'):
            with urlopen(url + path, timeout=3) as response: assert response.status == 200
        with urlopen(url + '/config.json', timeout=3) as response:
            assert json.load(response) == {'llm': True, 'enabled': True, 'model': 'local-fixture', 'timeout_s': 3}
        with urlopen(Request(url + '/ai/chat/completions?endpoint=http://ignored.invalid',
                data=json.dumps({'model': 'local-fixture', 'messages': [{'role': 'user', 'content': '查询状态'}]}).encode(),
                headers={'Content-Type': 'application/json', 'Authorization': 'Bearer browser-untrusted', 'Origin': url}), timeout=5) as response:
            assert 'choices' in json.load(response)
        with urlopen(api + '/test/requests', timeout=3) as response:
            observed = json.load(response)[-1]
        assert observed['auth_valid'] and observed['origin'] is None and observed['cookie'] is None
        assert urlsplit(observed['path']).path == '/no-cors'
        assert parse_qs(urlsplit(observed['path']).query) == {'literal': ['$host']}
        for method, headers, status in (
            ('GET', {}, 405),
            ('POST', {'Content-Type': 'text/plain'}, 415),
            ('POST', {'Content-Type': 'application/json', 'Origin': 'https://untrusted.invalid'}, 403),
        ):
            try:
                urlopen(Request(url + '/ai/chat/completions', data=b'{}' if method == 'POST' else None,
                                method=method, headers=headers), timeout=3)
                raise AssertionError('AI proxy accepted an invalid request')
            except HTTPError as exc:
                assert exc.code == status
                assert 'local-test-only' not in exc.read().decode()
        for message, status in [('认证错误', 401), ('其他错误', 418)]:
            try:
                urlopen(Request(url + '/ai/chat/completions',
                    data=json.dumps({'messages': [{'content': message}]}).encode(),
                    headers={'Content-Type': 'application/json'}), timeout=3)
                raise AssertionError('AI proxy accepted provider failure')
            except HTTPError as exc:
                assert exc.code == status
                assert 'local-test-only' not in exc.read().decode()
        try:
            urlopen(Request(url + '/health', headers={'Host': 'untrusted.invalid'}), timeout=3)
            raise AssertionError('WebUI proxy accepted an untrusted Host')
        except HTTPError as exc:
            assert exc.code == 421
        inspected = json.loads(subprocess.check_output(['docker', 'inspect', web_id], text=True))[0]
        assert inspected['HostConfig']['ReadonlyRootfs']
        assert inspected['HostConfig']['IpcMode'] != 'host'
        assert all('docker.sock' not in x['Destination'] and x['Destination'] != '/run/robot-control'
                   and not x['Destination'].startswith('/run/robot-control/') for x in inspected['Mounts'])
        assert any(x['Destination'] == '/run/robot-control-webui/config.json' and not x['RW'] for x in inspected['Mounts'])
        subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'sh', args.image,
                        '-c', 'test ! -e /usr/share/nginx/html/config.json && ! command -v node'], check=True, timeout=30)
        state = {'pid': os.getpid(), 'project': context['base_project'], 'deployment_id': context['deployment_id'],
                 'root': str(root), 'url': url, 'api': api, 'rc_container': rc_id, 'web_container': web_id,
                 'checks': ['production overlay', 'non-root read-only Nginx', 'sanitized public config', 'private config outside webroot',
                            'no-CORS AI proxy with server-owned Authorization', 'MCP token proxy', 'SPA route', 'no Node/key in runtime image']}
        (evidence / 'state.json').write_text(json.dumps(state, indent=2))
        ready = True
        print(json.dumps(state), flush=True)
        while not stopped.wait(.5): pass
    finally:
        if command is not None:
            for name in ('robot-control', 'webui'):
                result = subprocess.run(command + ['logs', '--no-color', name], text=True, capture_output=True, timeout=15)
                (evidence / f'{name}.log').write_text(result.stdout + result.stderr)
            exit_code = down(root, ['-f', 'compose.webui.yaml', '-f', 'override.json', 'down', '-v', '--remove-orphans'])
            remaining = subprocess.check_output(['docker', 'ps', '-aq', '--filter', f"label=com.docker.compose.project={context['base_project']}"], text=True).split()
            policies = subprocess.check_output(['docker', 'ps', '-aq', '--filter', f"label=io.robot-control.deployment={context['deployment_id']}"], text=True).split()
            volumes = subprocess.check_output(['docker', 'volume', 'ls', '-q', '--filter', f"label=com.docker.compose.project={context['base_project']}"], text=True).split()
            (evidence / 'cleanup.json').write_text(json.dumps({'exit_code': exit_code, 'containers': remaining, 'policies': policies, 'volumes': volumes}))
            assert (exit_code == 0 or not ready) and not remaining and not policies and not volumes
        shutil.rmtree(root)


if __name__ == '__main__': main()
