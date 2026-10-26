import base64
import io
import json
from pathlib import Path
import sys

import numpy as np
from PIL import Image
import pytest

pytest.importorskip('openai', reason='provider tests require the optional LLM policy environment')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packages/llm-direction-policy/src'))
from rc_llm_direction.actions import Decision
from rc_llm_direction.providers import ProviderClient
from rc_llm_direction.errors import ProviderError
from rc_llm_direction.runtime import DirectionPolicy, encode_images
from test_llm_direction import manifest, images, baseline, Runtime
from llm_http_helpers import model_server, response_for


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setenv('LLM_TEST_KEY', 'private-test-key-never-log')
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    with model_server() as server:
        yield server


def settings(server, provider='deepseek', format=None, timeout=2.):
    return {'type': provider, 'model': 'test-vision', 'base_url': server.url,
            'api_key_env': 'LLM_TEST_KEY', 'timeout_s': timeout,
            'output_format': format or {'deepseek': 'json_object', 'openai': 'json_schema',
                                       'openai_decisions': 'choice', 'openai_compatible': 'json_object'}[provider]}


@pytest.mark.parametrize('provider,format,path', [
    ('deepseek', 'json_object', '/v1/chat/completions'),
    ('openai_compatible', 'json_object', '/v1/chat/completions'),
    ('openai_compatible', 'json_schema', '/v1/chat/completions'),
    ('openai_compatible', 'text', '/v1/chat/completions'),
    ('openai', 'json_schema', '/v1/responses'),
    ('openai_decisions', 'choice', '/v1/decisions'),
])
def test_real_sdk_sends_three_named_images_and_parses_decision(server, provider, format, path):
    server.responses.append({'body': response_for(provider)})
    client = ProviderClient(settings(server, provider, format))
    encoded = encode_images(images(), manifest()['params'])
    try:
        assert client.decide('TASK_marker', 'PROMPT_marker', encoded) == Decision('up', 'noop', False)
    finally:
        client.close()
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request['path'] == path
    text = json.dumps(request['body'])
    assert 'TASK_marker' in text and 'PROMPT_marker' in text
    assert all(name in text for name in ('head', 'left_wrist', 'right_wrist'))
    assert text.count('data:image/jpeg;base64,') == 3
    if provider == 'openai_decisions':
        assert request['body']['input'][0]['role'] == 'user'
        parts = request['body']['input'][0]['content']
        assert sum(part['type'] == 'input_image' for part in parts) == 3
        questions = request['body']['questions']
        assert len(questions) == 1
        choices = [item['value'] for item in questions[0]['choices']]
        assert len(choices) == len(set(choices)) == 226
        assert 'up|close' in choices and 'noop|noop' in choices and 'done' in choices
    elif provider == 'openai':
        assert request['body']['store'] is False
        assert request['body']['text']['format']['strict'] is True
    elif format == 'text':
        assert 'response_format' not in request['body']
    else:
        assert request['body']['response_format']['type'] == format


def test_images_resize_without_upscaling_and_do_not_borrow_views():
    params = manifest(image_max_side=10)['params']
    frames = images()
    frames['head'] = np.zeros((40, 80, 3), np.uint8)
    frames['left_wrist'] = np.zeros((2, 3, 3), np.uint8)
    encoded = encode_images(frames, params)
    frames['head'].fill(255)
    for name, shape in [('head', (10, 5)), ('left_wrist', (3, 2))]:
        with Image.open(io.BytesIO(base64.b64decode(encoded[name].split(',')[1]))) as picture:
            assert picture.size == shape
            assert np.asarray(picture).max() == 0


@pytest.mark.parametrize('provider', ['deepseek', 'openai', 'openai_decisions'])
def test_task_completion_maps_to_one_pause(server, provider):
    server.responses.append({'body': response_for(provider, {'left': 'noop', 'right': 'noop', 'done': True})})
    instance = DirectionPolicy(manifest(provider=settings(server, provider)))
    runtime = Runtime()
    try:
        output = instance.policy({'eef': baseline()}, [], images(), runtime)
    finally:
        instance.close()
    assert len(runtime.pauses) == 1 and output.shape == (1, 14)


@pytest.mark.parametrize('failure', ['status', 'disconnect', 'timeout', 'invalid_json', 'duplicate_key', 'refusal', 'invalid_enum'])
def test_errors_pause_without_retry_fallback_or_response_leak(server, failure, capsys):
    secret = 'private-test-key-never-log'
    body = response_for('deepseek')
    item = {'body': body}
    if failure == 'status':
        item = {'status': 429, 'body': {'error': {'message': secret, 'type': 'rate_limit_error'}}}
    elif failure == 'disconnect':
        item = {'disconnect': True}
    elif failure == 'timeout':
        item = {'delay': .25}
    elif failure == 'refusal':
        body['choices'][0]['message']['refusal'] = secret
    elif failure == 'invalid_enum':
        body['choices'][0]['message']['content'] = json.dumps({'left': secret, 'right': 'noop', 'done': False})
    else:
        body['choices'][0]['message']['content'] = (secret if failure == 'invalid_json' else
            '{"left":"up","left":"down","right":"noop","done":false}')
    server.responses.append(item)
    instance = DirectionPolicy(manifest(provider=settings(server, timeout=.06 if failure == 'timeout' else 2)))
    runtime = Runtime()
    try:
        output = instance.policy({'eef': baseline()}, [], images(), runtime)
    finally:
        instance.close()
    assert len(server.requests) == 1 and len(runtime.pauses) == 1
    assert secret not in capsys.readouterr().out + str(runtime.pauses)
    np.testing.assert_allclose(output[0], baseline(), atol=1e-7)


def test_initialization_requires_env_key_and_never_contacts_server(server, monkeypatch):
    monkeypatch.delenv('LLM_TEST_KEY')
    with pytest.raises(ValueError, match='LLM_TEST_KEY'):
        ProviderClient(settings(server))
    assert not server.requests
