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


def test_optional_diagnostic_callback_preserves_boolean_and_no_default_output(server, capsys):
    from rc_llm_failure.providers import ProviderClient
    metadata = []
    server.responses.append({'body': response_for('deepseek', {'failed': True})})
    provider = ProviderClient({'model': 'deepseek-flash', 'base_url': server.url,
                              'api_key': 'private-test-key-never-log', 'timeout_s': 2.},
                             diagnostics=metadata.append)
    try:
        assert provider.decide('task', 'prompt', snapshots()) is True
    finally:
        provider.close()
    assert capsys.readouterr().out == ''
    assert len(server.requests) == len(metadata) == 1
    value = metadata[0]
    assert value['provider'] == 'deepseek' and value['model'] == 'deepseek-flash'
    assert value['http_status'] == 200
    assert value['usage']['total_tokens'] == 20
    assert value['finish_reason'] == 'stop'
    assert value['parsed_failed'] is True
    assert json.loads(value['raw_response'])['choices'][0]['message']['content'] == '{"failed": true}'
    assert 0 <= value['http_duration_s'] < 2
    assert value['http_started_mono_s'] <= value['http_finished_mono_s']


def test_callback_failure_cannot_replace_successful_provider_decision(server):
    from rc_llm_failure.providers import ProviderClient
    def broken_callback(metadata):
        raise RuntimeError('diagnostic destination failed')
    server.responses.append({'body': response_for('deepseek', {'failed': False})})
    provider = ProviderClient({'model': 'deepseek-flash', 'base_url': server.url,
                              'api_key': 'private-test-key-never-log', 'timeout_s': 2.},
                             diagnostics=broken_callback)
    try:
        assert provider.decide('task', 'prompt', snapshots()) is False
    finally:
        provider.close()
    assert len(server.requests) == 1


@pytest.mark.parametrize('kind', ['http', 'refusal', 'content'])
def test_diagnostics_scrub_reflected_api_key_and_authorization(server, kind, capsys):
    from rc_llm_failure.errors import ProviderError
    from rc_llm_failure.runtime import FailurePolicy
    from llm_failure_helpers import Runtime, done
    secret, other_secret = 'private-test-key-never-log', 'authorization-credential-must-not-escape'
    body = response_for('deepseek', {'failed': False})
    echoed = f'{secret}\nAuthorization: Bearer {other_secret}'
    item = {'body': body}
    if kind == 'http':
        item = {'status': 503, 'body': {'error': {'message': echoed, 'Authorization': 'Bearer ' + other_secret}}}
    else:
        body['choices'][0]['message'][kind if kind == 'refusal' else 'content'] = echoed
    server.responses.append(item)
    config = manifest()
    config['params']['provider'].update(base_url=server.url, api_key=secret)
    instance, runtime = FailurePolicy(config), Runtime()
    chunk = [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        done(instance)
        with pytest.raises(ProviderError):
            instance.policy(chunk, {}, runtime)
    finally:
        instance.close()
    text = capsys.readouterr().out
    assert secret not in text and other_secret not in text
    values = [json.loads(line) for line in text.splitlines()]
    terminal = next(value for value in values if value['event'] == 'llm_failure.request.error')
    assert '[REDACTED]' in terminal['raw_response']
    assert isinstance(json.loads(terminal['raw_response']), dict)
    assert terminal['http_status'] == (503 if kind == 'http' else 200)
    assert terminal['error']


def test_http_duration_excludes_completed_response_waiting_for_collection(server, monkeypatch, capsys):
    import threading
    import rc_llm_failure.runtime as runtime_module
    import rc_llm_failure.providers as providers_module
    from llm_failure_helpers import Runtime, done
    clock, gate = [100.], threading.Event()
    monkeypatch.setattr(runtime_module, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(providers_module, 'monotonic', lambda: clock[0], raising=False)
    server.responses.append({'wait': gate, 'body': response_for('deepseek', {'failed': False})})
    config = manifest()
    config['params']['provider'].update(base_url=server.url, timeout_s=30.)
    instance, runtime = runtime_module.FailurePolicy(config), Runtime()
    chunk = [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        assert server.received.wait(2)
        clock[0] = 102.
        gate.set()
        done(instance)
        clock[0] = 120.
        instance.policy(chunk, {}, runtime)
        values = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        terminal = next(value for value in values if value['event'] == 'llm_failure.request.completed')
        collected = values[-1]
        assert terminal['http_duration_s'] == 2.
        assert terminal['http_started_mono_s'] == 100.
        assert terminal['http_finished_mono_s'] == terminal['finished_mono_s'] == 102.
        assert collected['collection_wait_s'] == 18.
        assert collected['http_duration_s'] == 2.
        assert len(server.requests) == 1
    finally:
        gate.set()
        instance.close()


def test_escaped_unicode_response_is_bounded_as_complete_json_lines(server, capsys):
    from rc_llm_failure.errors import ProviderError
    from rc_llm_failure.runtime import FailurePolicy
    from llm_failure_helpers import Runtime, done
    body = response_for('deepseek', {'failed': False})
    body['choices'][0]['message']['content'] = '\x00\\"失败💥' * 5000
    server.responses.append({'body': body})
    config = manifest()
    config['params']['provider']['base_url'] = server.url
    instance, runtime = FailurePolicy(config), Runtime()
    chunk = [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        done(instance)
        with pytest.raises(ProviderError):
            instance.policy(chunk, {}, runtime)
    finally:
        instance.close()
    lines = capsys.readouterr().out.splitlines()
    assert all(len(line.encode('utf-8')) + 1 <= 8000 for line in lines)
    values = [json.loads(line) for line in lines]
    terminal = next(value for value in values if value['event'] == 'llm_failure.request.error')
    assert terminal['truncated'] is True
    assert 'raw_response' in terminal['truncated_fields']
    assert terminal['original_size_bytes'] > 8000
    assert terminal['raw_response_original_size_bytes'] > 8000


@pytest.mark.parametrize('status', [200, 503])
def test_provider_request_id_and_http_status_are_preserved(server, monkeypatch, status):
    from rc_llm_failure.errors import ProviderError
    from rc_llm_failure.providers import ProviderClient
    original = server.RequestHandlerClass.end_headers
    def end_headers(handler):
        handler.send_header('x-request-id', 'provider-request-fixture-123')
        original(handler)
    monkeypatch.setattr(server.RequestHandlerClass, 'end_headers', end_headers)
    body = response_for('deepseek', {'failed': False}) if status == 200 else {'error': {'message': 'unavailable'}}
    server.responses.append({'status': status, 'body': body})
    metadata = []
    provider = ProviderClient({'model': 'deepseek-flash', 'base_url': server.url,
                              'api_key': 'private-test-key-never-log', 'timeout_s': 2.},
                             diagnostics=metadata.append)
    try:
        if status == 200:
            assert provider.decide('task', 'prompt', snapshots()) is False
        else:
            with pytest.raises(ProviderError, match='503'):
                provider.decide('task', 'prompt', snapshots())
    finally:
        provider.close()
    assert len(metadata) == len(server.requests) == 1
    assert metadata[0]['provider_request_id'] == 'provider-request-fixture-123'
    assert metadata[0]['http_status'] == status
    assert json.loads(metadata[0]['raw_response']) == body


def test_http_error_duration_excludes_diagnostic_body_processing(server, monkeypatch):
    import rc_llm_failure.providers as module
    from rc_llm_failure.errors import ProviderError
    clock = [100.]
    monkeypatch.setattr(module, 'monotonic', lambda: clock[0])
    metadata = []
    provider = module.ProviderClient({'model': 'deepseek-flash', 'base_url': server.url,
                                     'api_key': 'private-test-key-never-log', 'timeout_s': 2.},
                                    diagnostics=metadata.append)
    capture = provider._capture_http
    def slow_capture(*args):
        clock[0] = 107.
        capture(*args)
    monkeypatch.setattr(provider, '_capture_http', slow_capture)
    server.responses.append({'status': 503, 'body': {'error': {'message': 'unavailable'}}})
    try:
        with pytest.raises(ProviderError):
            provider.decide('task', 'prompt', snapshots())
    finally:
        provider.close()
    assert metadata[0]['http_duration_s'] == 0.
    assert metadata[0]['http_finished_mono_s'] == 100.


@pytest.mark.parametrize('content', ['状态判断。' * 32, 'invalid Unicode: \ud800'])
def test_small_raw_response_is_retained_in_full_even_when_contract_is_invalid(server, capsys, content):
    from rc_llm_failure.errors import ProviderError
    from rc_llm_failure.runtime import FailurePolicy
    from llm_failure_helpers import Runtime, done
    body = response_for('deepseek', {'failed': False})
    body['choices'][0]['message']['content'] = content
    body['usage']['completion_tokens'] = 128
    server.responses.append({'body': body})
    config = manifest()
    config['params']['provider']['base_url'] = server.url
    instance, runtime = FailurePolicy(config), Runtime()
    chunk = [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        done(instance)
        with pytest.raises(ProviderError):
            instance.policy(chunk, {}, runtime)
    finally:
        instance.close()
    values = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    terminal = next(value for value in values if value['event'] == 'llm_failure.request.error')
    assert terminal['truncated'] is False
    assert json.loads(terminal['raw_response'])['choices'][0]['message']['content'] == content


def decision_settings(server=None, **patch):
    return {'enabled': True, 'model': 'gpt-test',
            'base_url': server.url if server else 'https://api.openai.com/v1',
            'api_key': 'decision-private-key', 'timeout_s': 2., **patch}


def decision_response(choice='failed'):
    return {'model': 'gpt-response', 'answers': [
        {'type': 'choice', 'name': 'failed', 'choice': choice, 'confidence': .9,
         'probabilities': [{'value': choice, 'probability': .9}]}],
        'usage': {'input_tokens': 10, 'output_tokens': 1, 'total_tokens': 11}}


@pytest.mark.parametrize('choice,expected', [('failed', True), ('not_failed', False)])
def test_decisions_real_sdk_preserves_history_and_diagnostics(server, monkeypatch, choice, expected):
    from rc_llm_failure.runtime import FailurePolicy
    from llm_failure_helpers import Runtime, done
    original = server.RequestHandlerClass.end_headers
    def headers(handler):
        handler.send_header('x-request-id', 'decision-request-123')
        original(handler)
    monkeypatch.setattr(server.RequestHandlerClass, 'end_headers', headers)
    server.responses.append({'body': decision_response(choice)})
    config = manifest(decision_api=decision_settings(server))
    config['params']['provider']['api_key'] = ''
    instance, runtime = FailurePolicy(config), Runtime()
    events = []
    instance._diagnostics.emit = events.append
    chunk = [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        done(instance)
        instance.policy(chunk, {}, runtime)
    finally:
        instance.close()
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request['path'] == '/v1/decisions'
    body = request['body']
    assert set(body) == {'model', 'input', 'questions'}
    question, = body['questions']
    assert question['type'] == 'choice' and question['name'] == 'failed'
    assert [item['value'] for item in question['choices']] == ['not_failed', 'failed']
    content = body['input'][0]['content']
    assert len([item for item in content if item['type'] == 'input_image']) == 6
    text = '\n'.join(item['text'] for item in content if item['type'] == 'input_text')
    assert text.index('step=0') < text.index('step=60')
    terminal = next(event for event in events if event['event'] == 'llm_failure.request.completed')
    assert terminal['provider'] == 'openai_decisions'
    assert terminal['parsed_failed'] is expected
    assert terminal['provider_request_id'] == 'decision-request-123'
    assert terminal['usage'] == {'input_tokens': 10, 'output_tokens': 1, 'total_tokens': 11}
    assert terminal['http_status'] == 200 and terminal['http_duration_s'] >= 0
    assert json.loads(terminal['raw_response'])['model'] == 'gpt-response'
    assert bool(runtime.finishes) is expected


@pytest.mark.parametrize('choice', [True, False, 1, None, 'true', 'unknown'])
def test_decisions_rejects_unoffered_choices_without_retry(server, choice):
    from rc_llm_failure.providers import ProviderClient
    from rc_llm_failure.errors import ProviderError
    server.responses.append({'body': decision_response(choice)})
    settings = decision_settings(server)
    settings.pop('enabled')
    provider = ProviderClient({**settings, 'type': 'openai_decisions'})
    try:
        with pytest.raises(ProviderError, match='契约'):
            provider.decide('task', 'prompt', snapshots())
    finally:
        provider.close()
    assert len(server.requests) == 1


@pytest.mark.parametrize('patch', [{'enabled': 1}, {'api_key': 1}, {'timeout_s': True},
                                   {'model': ''}, {'base_url': 1}, {'unknown': 1}])
def test_inactive_decisions_still_validates_structure(patch):
    from rc_llm_failure.configuration import validate_config
    config = manifest(decision_api=decision_settings(**{'enabled': False, **patch}))
    with pytest.raises(ValueError):
        validate_config(config)


def test_only_active_backend_requires_nonempty_key():
    from rc_llm_failure.configuration import validate_config
    config = manifest(decision_api=decision_settings())
    config['params']['provider']['api_key'] = ''
    assert validate_config(config)['decision_api']['enabled'] is True
    config['params']['decision_api']['enabled'] = False
    with pytest.raises(ValueError, match='api_key'):
        validate_config(config)
    config['params']['provider']['api_key'] = 'deepseek-key'
    config['params']['decision_api']['api_key'] = ''
    assert validate_config(config)['decision_api']['enabled'] is False


@pytest.mark.parametrize('kind', ['http', 'timeout', 'refusal', 'wrong_name', 'extra_answer'])
def test_decisions_errors_keep_diagnostics_and_never_retry(server, kind):
    from rc_llm_failure.providers import ProviderClient
    from rc_llm_failure.errors import ProviderError
    body = decision_response()
    item = {'body': body}
    if kind == 'http':
        item = {'status': 503, 'body': {'error': {'message': 'decision-private-key'}}}
    elif kind == 'timeout':
        item = {'delay': .25}
    elif kind == 'refusal':
        body['answers'] = [{'type': 'refusal', 'name': 'failed'}]
    elif kind == 'wrong_name':
        body['answers'][0]['name'] = 'other'
    else:
        body['answers'].append(body['answers'][0])
    server.responses.append(item)
    metadata = []
    settings = decision_settings(server, timeout_s=.05 if kind == 'timeout' else 2.)
    settings.pop('enabled')
    provider = ProviderClient({**settings, 'type': 'openai_decisions'}, diagnostics=metadata.append)
    try:
        with pytest.raises(ProviderError) as error:
            provider.decide('task', 'prompt', snapshots())
    finally:
        provider.close()
    assert 'decision-private-key' not in str(error.value) + json.dumps(metadata)
    assert len(server.requests) == len(metadata) == 1
    assert metadata[0]['provider'] == 'openai_decisions'
    assert metadata[0]['error'] and metadata[0]['http_duration_s'] >= 0


def test_decisions_runtime_uses_selected_deadline_and_scrubs_both_keys(server, capsys):
    from rc_llm_failure.runtime import FailurePolicy
    from llm_failure_helpers import Runtime, done
    body = decision_response()
    body['usage']['input_tokens_details'] = {'cached_tokens': 8, 'cache_write_tokens': 0}
    body['usage']['output_tokens_details'] = {'reasoning_tokens': 1}
    body['extra'] = 'inactive-private-key decision-private-key'
    server.responses.append({'body': body})
    config = manifest(decision_api=decision_settings(server, timeout_s=7.))
    config['params']['provider'].update(api_key='inactive-private-key', timeout_s=.001)
    instance, runtime = FailurePolicy(config), Runtime()
    chunk = [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        request = instance._request
        assert request['deadline'] - request['started'] == pytest.approx(7.)
        done(instance)
        instance.policy(chunk, {}, runtime)
    finally:
        instance.close()
    text = capsys.readouterr().out
    assert 'inactive-private-key' not in text and 'decision-private-key' not in text
    events = [json.loads(line) for line in text.splitlines()]
    assert [event['event'] for event in events] == [
        'llm_failure.request.started', 'llm_failure.request.completed', 'llm_failure.collection.accepted']
    assert all(event['provider'] == 'openai_decisions' for event in events)
    assert events[1]['usage']['input_tokens_details']['cached_tokens'] == 8
    assert events[1]['usage']['output_tokens_details']['reasoning_tokens'] == 1


def test_decisions_reset_discards_late_true_and_recording_keeps_backend(server, tmp_path, capsys):
    import threading
    from rc_llm_failure.runtime import FailurePolicy
    from llm_failure_helpers import Runtime, wait
    from robot_control.recording_writer import SessionFiles
    gate = threading.Event()
    server.responses.append({'wait': gate, 'body': decision_response()})
    instance = FailurePolicy(manifest(decision_api=decision_settings(server)))
    runtime, chunk = Runtime(), [[0.] * 14]
    reset = None
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        assert server.received.wait(2)
        reset = threading.Thread(target=instance.reset_context)
        reset.start()
        wait(lambda: instance._generation == 1)
        gate.set()
        reset.join(3)
        assert not reset.is_alive()
        instance.policy(chunk, images(), runtime)
        assert runtime.finishes == []
    finally:
        gate.set()
        if reset:
            reset.join(3)
        instance.close()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [event['event'] for event in events] == [
        'llm_failure.request.started', 'llm_failure.request.completed', 'llm_failure.collection.discarded']
    assert events[-1]['reason'] == 'reset' and events[-1]['parsed_failed'] is True
    session = SessionFiles(tmp_path / 'session', 's', {'run_id': 'decision-run'})
    try:
        for event in events:
            session.event('events', {'event': 'policy.stdout', 'message': json.dumps(event),
                                    'instance_id': 'llm_failure-0', 'run_id': 'decision-run'})
    finally:
        session.close()
    saved = [json.loads(line) for line in (session.root / 'llm_failure.jsonl').read_text().splitlines()]
    assert len(saved) == 3
    assert all(event['provider'] == 'openai_decisions' for event in saved)
    assert all(event['policy_instance_id'] == 'llm_failure-0' for event in saved)
    assert saved[1]['parsed_failed'] is True
    assert len(server.requests) == 1
