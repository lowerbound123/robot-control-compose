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


def render(tmp_path, config, *, raw=False, upstream=None, hosts=None):
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
test "$(stat -c %a /tmp/webui-rc.conf)" = 600
cat /tmp/webui-public.json
"""
    options = []
    if upstream is not None:
        options += ['--env', f'RC_WEBUI_UPSTREAM={upstream}']
    if hosts is not None:
        hosts_path = tmp_path / 'hosts'
        hosts_path.write_text(hosts)
        options += ['--mount', f'type=bind,source={hosts_path},target=/etc/hosts,readonly']
    return subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--read-only',
        '--user', f'{os.getuid()}:{os.getgid()}', '--tmpfs', '/tmp:rw,mode=1777',
        '--mount', f'type=bind,source={path},target=/run/robot-control-webui/config.json,readonly',
        *options, '--entrypoint', 'sh', IMAGE, '-c', command], text=True, capture_output=True, timeout=15)


@pytest.mark.parametrize('endpoint,model', [('', ''), ('https://model.example:8443/v1/chat/completions', '模型'),
                                           ('http://127.0.0.1:8000/v1?x=$host&version=1', 'fixture')])
def test_real_njs_generates_valid_nginx_and_sanitized_public_config(tmp_path, endpoint, model):
    result = render(tmp_path, dict(endpoint=endpoint, model=model, api_key='private-render-canary', timeout_s=.5))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == dict(llm=True, enabled=bool(endpoint and model), model=model, timeout_s=.5, effort=None)
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
    assert json.loads(result.stdout) == dict(llm=False, enabled=False, model='', timeout_s=30, effort=None)


@pytest.mark.parametrize('effort', [None, 'none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'])
def test_real_njs_projects_optional_effort(tmp_path, effort):
    result = render(tmp_path, {'endpoint': 'https://model.example/v1', 'model': 'fixture',
                              'api_key': '', 'timeout_s': 30, 'effort': effort})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['effort'] == effort


@pytest.mark.parametrize('effort', ['private-render-canary', ['high'], True])
def test_real_njs_rejects_invalid_effort_without_echo(tmp_path, effort):
    result = render(tmp_path, {'llm': False, 'effort': effort})
    assert result.returncode != 0
    assert 'private-render-canary' not in result.stdout + result.stderr


def test_real_host_gateway_render_keeps_upstream_out_of_public_config(tmp_path):
    result = render(tmp_path, {'llm': False}, upstream='http://host.docker.internal:4000',
                    hosts='127.0.0.1 localhost\n192.0.2.1 host.docker.internal\n')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == dict(llm=False, enabled=False, model='', timeout_s=30, effort=None)
    assert '192.0.2.1' not in result.stdout and 'host.docker.internal' not in result.stdout


@pytest.mark.parametrize('upstream,hosts', [
    ('http://host.docker.internal:4000', '127.0.0.1 localhost\n'),
    ('http://user:private-upstream-canary@rc:4000', None),
    ('http://rc:4000;private-upstream-canary', None),
    ('http://rc:4000\nprivate-upstream-canary', None),
])
def test_real_invalid_rc_upstream_fails_before_nginx_without_echo(tmp_path, upstream, hosts):
    result = render(tmp_path, {'llm': False}, upstream=upstream, hosts=hosts)
    assert result.returncode != 0
    assert 'Cannot load WebUI RC upstream' in result.stderr
    assert 'private-upstream-canary' not in result.stdout + result.stderr
