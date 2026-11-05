"""Real Docker/Nginx forwarding; host-network backend is a disposable HTTP fixture.

Set RC_WEBUI_TEST_IMAGE to the built WebUI image. No ROS or hardware is used.
"""
import json
import os
import subprocess
import time
from urllib.request import Request, ProxyHandler, build_opener
from uuid import uuid4

import pytest

IMAGE = os.environ.get('RC_WEBUI_TEST_IMAGE')
BACKEND = os.environ.get('RC_WEBUI_TEST_BACKEND_IMAGE', 'robot-control-compose-rc:local')
pytestmark = pytest.mark.skipif(not IMAGE, reason='set RC_WEBUI_TEST_IMAGE for Docker network checks')

BACKEND_CODE = r'''
import json, os, time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self): self.do_GET()
    def do_GET(self):
        if self.path == '/ai-fixture':
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
            if (self.headers.get('Authorization') != 'Bearer ai-stream-test'
                    or 'text/event-stream' not in self.headers.get('Accept', '') or body.get('stream') is not True):
                self.send_error(400); return
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Set-Cookie', 'private-stream-canary')
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n')
            self.wfile.flush()
            deadline = time.monotonic() + 10
            while not Path('/tmp/release-stream').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
            return
        if self.headers.get('Authorization') != 'Bearer rc-network-test':
            self.send_error(401); return
        if self.headers.get('Host') != 'localhost:4000':
            self.send_error(421); return
        body = self.rfile.read(int(self.headers.get('Content-Length', '0'))).decode()
        if self.path.startswith('/preview/') and self.path.endswith('.mjpg'):
            content_type = 'multipart/x-mixed-replace; boundary=frame'
            data = b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 4\r\n\r\n\xff\xd8\xff\xd9\r\n--frame--\r\n'
        else:
            content_type = 'application/json'
            data = json.dumps(dict(path=self.path, method=self.command, body=body,
                                   host=self.headers['Host'], auth=self.headers['Authorization'])).encode()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)
server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT', '0'))), Handler)
print(server.server_port, flush=True)
server.serve_forever()
'''


def docker(*args, check=True):
    return subprocess.run(['docker', *args], text=True, capture_output=True, check=check, timeout=30)


@pytest.mark.parametrize('mode', ['bridge', 'host'])
def test_real_proxy_forwards_health_mcp_and_three_camera_streams(tmp_path, mode):
    prefix = 'rc-webui-net-' + uuid4().hex[:10]
    network, backend, web = prefix, prefix + '-backend', prefix + '-web'
    config = tmp_path / 'config.json'
    config.write_text('{"llm":false}')
    config.chmod(0o600)
    opener = build_opener(ProxyHandler({}))
    try:
        docker('network', 'create', network)
        backend_network = (['--network', 'host'] if mode == 'host' else
                           ['--network', network, '--network-alias', 'robot-control', '--env', 'PORT=4000'])
        docker('run', '-d', '--pull=never', '--name', backend, *backend_network,
               '--entrypoint', 'python3', BACKEND, '-u', '-c', BACKEND_CODE)
        port = None
        for _ in range(100):
            output = docker('logs', backend).stdout.strip()
            if output.isdigit():
                port = int(output)
                break
            time.sleep(.05)
        assert port is not None, docker('logs', backend).stderr
        upstream = (['--env', f'RC_WEBUI_UPSTREAM=http://host.docker.internal:{port}']
                    if mode == 'host' else [])
        docker('run', '-d', '--pull=never', '--name', web, '--network', network,
               '--add-host', 'host.docker.internal:host-gateway', *upstream,
               '--env', 'ROBOT_CONTROL_MCP_TOKEN=rc-network-test',
               '--user', f'{os.getuid()}:{os.getgid()}', '--read-only',
               '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,mode=1777,size=16m',
               '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
               '--mount', f'type=bind,source={config},target=/run/robot-control-webui/config.json,readonly',
               '-p', '127.0.0.1::8080', IMAGE)
        address = 'http://' + docker('port', web, '8080/tcp').stdout.strip()
        for _ in range(100):
            try:
                with opener.open(address + '/monitor', timeout=.5) as reply:
                    if reply.status == 200:
                        break
            except OSError:
                time.sleep(.05)
        else:
            pytest.fail('WebUI did not become ready: ' + docker('logs', web).stderr)
        for path, body in [('/health', None), ('/preview/status', None),
                           ('/mcp', b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')]:
            with opener.open(Request(address + path, data=body, headers={'Content-Type': 'application/json'}),
                             timeout=5) as reply:
                result = json.load(reply)
            assert result == dict(path=path, method='GET' if body is None else 'POST',
                                  body='' if body is None else body.decode(),
                                  host='localhost:4000', auth='Bearer rc-network-test')
        for camera in ('head', 'left_wrist', 'right_wrist'):
            with opener.open(address + f'/preview/{camera}.mjpg', timeout=5) as reply:
                assert reply.headers['Content-Type'] == 'multipart/x-mixed-replace; boundary=frame'
                assert reply.read() == (b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 4\r\n\r\n'
                                        b'\xff\xd8\xff\xd9\r\n--frame--\r\n')
        with opener.open(address + '/config.json', timeout=5) as reply:
            assert json.load(reply) == dict(llm=False, enabled=False, model='', timeout_s=30, effort=None)
    finally:
        docker('rm', '-f', web, backend, check=False)
        docker('network', 'rm', network, check=False)


def test_real_nginx_delivers_sse_before_the_provider_is_allowed_to_finish(tmp_path):
    prefix = 'rc-webui-sse-' + uuid4().hex[:10]
    network, backend, web = prefix, prefix + '-backend', prefix + '-web'
    config = tmp_path / 'config.json'
    config.write_text(json.dumps(dict(endpoint='http://robot-control:4000/ai-fixture', model='test',
                                      api_key='ai-stream-test', timeout_s=5)))
    config.chmod(0o600)
    opener = build_opener(ProxyHandler({}))
    try:
        docker('network', 'create', network)
        docker('run', '-d', '--pull=never', '--name', backend, '--network', network,
               '--network-alias', 'robot-control', '--env', 'PORT=4000',
               '--entrypoint', 'python3', BACKEND, '-u', '-c', BACKEND_CODE)
        for _ in range(100):
            if docker('logs', backend).stdout.strip() == '4000':
                break
            time.sleep(.05)
        else:
            pytest.fail('SSE fixture did not start')
        docker('run', '-d', '--pull=never', '--name', web, '--network', network,
               '--read-only', '--tmpfs', '/tmp:rw,nosuid,nodev,noexec,mode=1777,size=16m',
               '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
               '--mount', f'type=bind,source={config},target=/run/robot-control-webui/config.json,readonly',
               '-p', '127.0.0.1::8080', IMAGE)
        address = 'http://' + docker('port', web, '8080/tcp').stdout.strip()
        for _ in range(100):
            try:
                with opener.open(address + '/monitor', timeout=.5):
                    break
            except OSError:
                time.sleep(.05)
        else:
            pytest.fail('Nginx did not become ready')
        with opener.open(Request(address + '/ai/chat/completions', data=b'{"stream":true}',
                                 headers={'Content-Type': 'application/json'}), timeout=3) as reply:
            assert reply.headers['Content-Type'].startswith('text/event-stream')
            assert reply.headers.get('Set-Cookie') is None
            assert reply.readline() == b'data: {"choices":[{"delta":{"content":"first"}}]}\n'
            assert reply.readline() == b'\n'
            # The provider cannot finish until AFTER we have read the first chunk.
            docker('exec', backend, 'touch', '/tmp/release-stream')
            assert reply.read() == b'data: [DONE]\n\n'
    finally:
        docker('rm', '-f', web, backend, check=False)
        docker('network', 'rm', network, check=False)
