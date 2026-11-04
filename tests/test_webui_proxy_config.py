"""Validate the real pinned njs renderer and Nginx with disposable private files.

Set RC_WEBUI_TEST_IMAGE to an already built WebUI image. No provider is contacted.
"""
import json
import os
from pathlib import Path
import subprocess

import pytest

IMAGE = os.environ.get('RC_WEBUI_TEST_IMAGE')
pytestmark = pytest.mark.skipif(not IMAGE, reason='set RC_WEBUI_TEST_IMAGE for real njs/Nginx configuration checks')


def render(tmp_path, config, *, raw=False):
    path = tmp_path / 'config.json'
    path.write_text(config if raw else json.dumps(config, ensure_ascii=False))
    path.chmod(0o600)
    # Only the public metadata is printed. The generated secret-bearing proxy
    # configuration remains in the disposable container's private /tmp.
    command = """set -e
njs -m /etc/nginx/webui/render-config.mjs
envsubst '${RC_MCP_AUTHORIZATION}' < /etc/nginx/rc.conf.template > /tmp/rc-nginx.conf
nginx -t -c /tmp/rc-nginx.conf
test ! -e /usr/share/nginx/html/config.json
test "$(stat -c %a /tmp/webui-ai.conf)" = 600
cat /tmp/webui-public.json
"""
    return subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--read-only',
        '--user', f'{os.getuid()}:{os.getgid()}', '--tmpfs', '/tmp:rw,mode=1777',
        '--mount', f'type=bind,source={path},target=/run/robot-control-webui/config.json,readonly',
        '--entrypoint', 'sh', IMAGE, '-c', command], text=True, capture_output=True, timeout=15)


@pytest.mark.parametrize('endpoint,model', [('', ''), ('https://model.example:8443/v1/chat/completions', '模型'),
                                           ('http://127.0.0.1:8000/v1?x=$host&version=1', 'fixture')])
def test_real_njs_generates_valid_nginx_and_sanitized_public_config(tmp_path, endpoint, model):
    result = render(tmp_path, dict(endpoint=endpoint, model=model, api_key='private-render-canary', timeout_s=.5))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == dict(llm=True, enabled=bool(endpoint and model), model=model, timeout_s=.5)
    assert 'private-render-canary' not in result.stdout + result.stderr
    assert endpoint not in result.stdout if endpoint else True


@pytest.mark.parametrize('patch', [{'endpoint': 'https://@invalid.example'},
                                  {'endpoint': 'https://model.example/";private-render-canary;#'},
                                  {'api_key': 'private-render-canary\r\nInjected: true'},
                                  {'timeout_s': 0}, {'timeout_s': 121}])
def test_invalid_server_config_fails_before_nginx_without_echoing_secrets(tmp_path, patch):
    config = dict(endpoint='https://model.example/v1/chat/completions', model='fixture',
                  api_key='private-render-canary', timeout_s=30)
    result = render(tmp_path, {**config, **patch})
    assert result.returncode != 0
    assert 'private-render-canary' not in result.stdout + result.stderr


def test_invalid_json_diagnostic_does_not_print_file_contents(tmp_path):
    result = render(tmp_path, '{"api_key":"private-render-canary",', raw=True)
    assert result.returncode != 0
    assert 'private-render-canary' not in result.stdout + result.stderr


def test_direct_mode_needs_no_provider_config(tmp_path):
    result = render(tmp_path, {'llm': False})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == dict(llm=False, enabled=False, model='', timeout_s=30)
