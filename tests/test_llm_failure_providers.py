import json

import pytest

pytest.importorskip('openai', reason='provider tests require the optional LLM environment')
from llm_failure_helpers import images, manifest
from llm_http_helpers import model_server, response_for


@pytest.fixture
def server():
    with model_server() as value:
        yield value


def client(server, timeout=2.):
    from rc_llm_failure.providers import ProviderClient
    return ProviderClient({'model': 'deepseek-flash', 'base_url': server.url,
                           'api_key': 'private-test-key-never-log', 'timeout_s': timeout})


def snapshots():
    from rc_llm_failure.images import encode_snapshot
    return tuple(encode_snapshot(images(20 + step), step, manifest()['params']) for step in (0, 60))


@pytest.mark.parametrize('failed', [False, True])
def test_real_sdk_sends_chronological_images_json_and_no_thinking(server, failed):
    server.responses.append({'body': response_for('deepseek', {'failed': failed})})
    provider = client(server)
    try:
        assert not server.requests, 'initialization must not make paid requests'
        assert provider.decide('TASK_marker', 'PROMPT_marker', snapshots()) is failed
    finally:
        provider.close()
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request['path'] == '/v1/chat/completions'
    body = request['body']
    assert body['model'] == 'deepseek-flash'
    assert body['thinking'] == {'type': 'disabled'}
    assert body['response_format'] == {'type': 'json_object'}
    assert body['temperature'] == 0 and body['max_tokens'] == 128
    assert not body.get('stream') and not body.get('tools')
    assert body['messages'][0]['role'] == 'system'
    assert 'PROMPT_marker' in body['messages'][0]['content']
    assert 'JSON' in body['messages'][0]['content'] and 'failed' in body['messages'][0]['content']
    content = body['messages'][1]['content']
    assert body['messages'][1]['role'] == 'user'
    assert 'TASK_marker' in content[0]['text']
    encoded = [part['image_url']['url'] for part in content if part['type'] == 'image_url']
    assert encoded == [data for sample in snapshots() for _, data in sample.images]
    text = '\n'.join(part['text'] for part in content if part['type'] == 'text')
    assert text.index('step=0') < text.index('step=60')
    assert all(text.count(camera) == 2 for camera in ('head', 'left_wrist', 'right_wrist'))


@pytest.mark.parametrize('text', [
    '', 'not JSON', '```json\n{"failed":true}\n```', '{"failed":true} explanation',
    '{"failed":"true"}', '{"failed":1}', '{"failed":null}', '{"failed":NaN}',
    '{"failed":false,"message":"extra"}', '{"failed":true,"failed":false}', '[]', '{}',
])
def test_only_an_exact_boolean_json_object_is_accepted(server, text):
    from rc_llm_failure.errors import ProviderError
    body = response_for('deepseek', {'failed': False})
    body['choices'][0]['message']['content'] = text
    server.responses.append({'body': body})
    provider = client(server)
    try:
        with pytest.raises(ProviderError, match='契约'):
            provider.decide('task', 'prompt', snapshots())
    finally:
        provider.close()
    assert len(server.requests) == 1


@pytest.mark.parametrize('kind', ['http', 'disconnect', 'timeout', 'refusal', 'truncated', 'extra_choice'])
def test_api_errors_never_retry_or_expose_response_contents(server, kind, capsys):
    from rc_llm_failure.errors import ProviderError
    secret = 'private-test-key-never-log'
    body = response_for('deepseek', {'failed': False})
    item = {'body': body}
    if kind == 'http':
        item = {'status': 503, 'body': {'error': {'message': secret}}}
    elif kind == 'disconnect':
        item = {'disconnect': True}
    elif kind == 'timeout':
        item = {'delay': .25}
    elif kind == 'refusal':
        body['choices'][0]['message']['refusal'] = secret
    elif kind == 'truncated':
        body['choices'][0]['finish_reason'] = 'length'
    else:
        body['choices'].append(body['choices'][0])
    server.responses.append(item)
    provider = client(server, timeout=.05 if kind == 'timeout' else 2)
    try:
        with pytest.raises(ProviderError) as failure:
            provider.decide('task', 'prompt', snapshots())
        assert secret not in str(failure.value) + capsys.readouterr().out
    finally:
        provider.close()
    assert len(server.requests) == 1


def test_oversized_request_is_rejected_before_network(server):
    from rc_llm_failure.images import Snapshot, MAX_REQUEST_BYTES
    from rc_llm_failure.errors import ProviderError
    provider = client(server)
    try:
        with pytest.raises(ProviderError, match='32MiB'):
            provider.decide('task', 'prompt', (Snapshot(0, (('head', 'x' * MAX_REQUEST_BYTES),)),))
    finally:
        provider.close()
    assert not server.requests


@pytest.mark.parametrize('patch', [
    {'api_key': ''}, {'api_key': 1}, {'model': ''}, {'timeout_s': True}, {'timeout_s': 0},
    {'timeout_s': float('inf')}, {'base_url': 'https://user:secret@example.com'},
    {'base_url': 'https://example.com?api_key=secret'}, {'base_url': 'file:///tmp/socket'},
    {'unknown': 1},
])
def test_provider_configuration_rejects_bad_settings(patch):
    from rc_llm_failure.configuration import validate_config
    config = manifest()
    config['params']['provider'].update(patch)
    with pytest.raises(ValueError):
        validate_config(config)
