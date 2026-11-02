import json
import threading

import pytest

pytest.importorskip('openai', reason='provider tests require the optional LLM environment')
from llm_failure_helpers import images, manifest, provider_settings
from llm_http_helpers import model_server, response_for


@pytest.fixture
def server():
    with model_server() as value:
        yield value


def client(server, timeout=2., **settings):
    from rc_llm_failure.providers import ProviderClient
    return ProviderClient(provider_settings(base_url=server.url, api_key='private-test-key-never-log',
                                            timeout_s=timeout, **settings))


def snapshots():
    from rc_llm_failure.images import encode_snapshot
    return tuple(encode_snapshot(images(20 + step), step, manifest()['params']) for step in (0, 60))


@pytest.mark.parametrize('decisions', [False, True])
@pytest.mark.parametrize('suffix', ['输出规则', ''])
def test_profile_owns_every_model_facing_string(tmp_path, server, decisions, suffix):
    from rc_llm_failure.configuration import load_provider
    from rc_llm_failure.providers import ProviderClient

    settings = decision_settings(server) if decisions else provider_settings(base_url=server.url)
    settings.update(task='夹取 {水果}', prompt='判断规则 {"failed":false}', response_instructions=suffix,
                    input_templates={'task': '目标={task}', 'history_snapshot': '过去/{index}/{step}',
                                     'current_snapshot': '现在/{index}/{step}', 'camera': '视角={camera}'})
    if decisions:
        settings['choice_descriptions'] = {'failed': '已偏离', 'not_failed': '可继续'}
    path = tmp_path / 'custom.yaml'
    path.write_text(json.dumps(settings))
    server.responses.append({'body': decision_response('not_failed') if decisions else response_for('deepseek', {'failed': False})})
    loaded = load_provider(path.name, tmp_path)
    provider = ProviderClient(loaded)
    loaded['input_templates']['task'] = '下次加载才生效/{task}'
    try:
        assert provider.decide(snapshots()) is False
    finally:
        provider.close()
    body = server.requests[0]['body']
    expected = '判断规则 {"failed":false}' + ('\n\n输出规则' if suffix else '')
    if decisions:
        question, = body['questions']
        assert question['instructions'] == expected
        assert question['choices'] == [{'value': 'not_failed', 'description': '可继续'},
                                       {'value': 'failed', 'description': '已偏离'}]
        content = body['input'][0]['content']
    else:
        assert body['messages'][0]['content'] == expected
        content = body['messages'][1]['content']
    cameras = ['视角=head', '视角=left_wrist', '视角=right_wrist']
    assert [item['text'] for item in content if 'text' in item] == [
        '目标=夹取 {水果}', '过去/1/0', *cameras, '现在/2/60', *cameras]


@pytest.mark.parametrize('failed', [False, True])
def test_real_sdk_sends_chronological_images_json_and_no_thinking(server, failed):
    server.responses.append({'body': response_for('deepseek', {'failed': failed})})
    provider = client(server, task='TASK_marker', prompt='PROMPT_marker')
    try:
        assert not server.requests, 'initialization must not make paid requests'
        assert provider.decide(snapshots()) is failed
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
    '[' * 1500 + ']' * 1500, '', 'not JSON', '```json\n{"failed":true}\n```', '{"failed":true} explanation',
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
            provider.decide(snapshots())
    finally:
        provider.close()
    assert len(server.requests) == 1


@pytest.mark.parametrize('kind', ['http', 'disconnect', 'timeout', 'refusal', 'truncated', 'extra_choice'])
def test_api_errors_never_retry_or_expose_response_contents(server, kind, capsys):
    from rc_llm_failure.errors import ProviderError
    secret = 'private-test-key-never-log'
    body = response_for('deepseek', {'failed': False})
    gate = threading.Event()
    item = {'body': body}
    if kind == 'http':
        item = {'status': 503, 'body': {'error': {'message': secret}}}
    elif kind == 'disconnect':
        item = {'disconnect': True}
    elif kind == 'timeout':
        item = {'wait': gate}
    elif kind == 'refusal':
        body['choices'][0]['message']['refusal'] = secret
    elif kind == 'truncated':
        body['choices'][0]['finish_reason'] = 'length'
    else:
        body['choices'].append(body['choices'][0])
    server.responses.append(item)
    provider = client(server, timeout=2.)
    try:
        with pytest.raises(ProviderError) as failure:
            provider.decide(snapshots())
        if kind == 'timeout':
            assert server.received.is_set(), 'timeout must follow receipt by the mock HTTP server'
            assert 'API 请求超时' in str(failure.value)
        assert secret not in str(failure.value) + capsys.readouterr().out
    finally:
        gate.set()
        provider.close()
    assert len(server.requests) == 1


def test_oversized_request_is_rejected_before_network(server):
    from rc_llm_failure.images import Snapshot, MAX_REQUEST_BYTES
    from rc_llm_failure.errors import ProviderError
    provider = client(server)
    try:
        with pytest.raises(RuntimeError, match='32MiB'):
            provider.decide((Snapshot(0, (('head', 'x' * MAX_REQUEST_BYTES),)),))
    finally:
        provider.close()
    assert not server.requests


@pytest.mark.parametrize('patch', [
    {'api_key': ''}, {'api_key': 1}, {'model': ''}, {'timeout_s': True}, {'timeout_s': 0},
    {'timeout_s': float('inf')}, {'base_url': 'https://user:secret@example.com'},
    {'base_url': 'https://example.com?api_key=secret'}, {'base_url': 'file:///tmp/socket'},
    {'unknown': 1}, {'task': ' '}, {'prompt': None}, {'response_instructions': []},
    {'choice_descriptions': {'failed': 'not a Chat field'}},
])
def test_provider_configuration_rejects_bad_settings(patch, tmp_path):
    from rc_llm_failure.configuration import load_provider
    (tmp_path / 'provider.yaml').write_text(json.dumps(provider_settings(**patch)))
    with pytest.raises(ValueError):
        load_provider('provider.yaml', tmp_path)


@pytest.mark.parametrize(('field', 'text'), [
    ('task', 'no placeholder'), ('history_snapshot', '{step'), ('current_snapshot', '{index:>{step}}'),
    ('camera', '{camera.__class__}'), ('camera', '{api_key}'), ('camera', '{camera!r}'),
])
def test_invalid_prompt_template_rejected_before_switch(tmp_path, field, text):
    from rc_llm_failure.configuration import load_provider
    settings = provider_settings()
    settings['input_templates'][field] = text
    path = tmp_path / 'candidate.yaml'
    path.write_text(json.dumps(settings))
    with pytest.raises(ValueError, match='input_templates.' + field) as error:
        load_provider(path.name, tmp_path)
    assert settings['api_key'] not in str(error.value)


def test_decisions_requires_both_choice_descriptions(tmp_path):
    from rc_llm_failure.configuration import load_provider
    settings = decision_settings(choice_descriptions={'failed': 'yes'})
    (tmp_path / 'candidate.yaml').write_text(json.dumps(settings))
    with pytest.raises(ValueError, match='choice_descriptions'):
        load_provider('candidate.yaml', tmp_path)


def test_optional_diagnostic_callback_preserves_boolean_and_no_default_output(server, capsys):
    from rc_llm_failure.providers import ProviderClient
    metadata = []
    server.responses.append({'body': response_for('deepseek', {'failed': True})})
    provider = ProviderClient(provider_settings(base_url=server.url, api_key='private-test-key-never-log', timeout_s=2.),
                             diagnostics=metadata.append)
    try:
        assert provider.decide(snapshots()) is True
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
    provider = ProviderClient(provider_settings(base_url=server.url, api_key='private-test-key-never-log', timeout_s=2.),
                             diagnostics=broken_callback)
    try:
        assert provider.decide(snapshots()) is False
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
    instance, runtime = FailurePolicy(manifest(), provider_settings(base_url=server.url, api_key=secret)), Runtime()
    chunk = [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        done(instance)
        assert instance.policy(chunk, {}, runtime) is chunk
        assert instance.monitor()['failed_requests'] == 1
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
    instance, runtime = runtime_module.FailurePolicy(manifest(), provider_settings(base_url=server.url, timeout_s=30.)), Runtime()
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
    instance, runtime = FailurePolicy(manifest(), provider_settings(base_url=server.url)), Runtime()
    chunk = [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        done(instance)
        assert instance.policy(chunk, {}, runtime) is chunk
        assert instance.monitor()['failed_requests'] == 1
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
    provider = ProviderClient(provider_settings(base_url=server.url, api_key='private-test-key-never-log', timeout_s=2.),
                             diagnostics=metadata.append)
    try:
        if status == 200:
            assert provider.decide(snapshots()) is False
        else:
            with pytest.raises(ProviderError, match='503'):
                provider.decide(snapshots())
    finally:
        provider.close()
    assert len(metadata) == len(server.requests) == 1
    assert metadata[0]['provider_request_id'] == 'provider-request-fixture-123'
    assert metadata[0]['http_status'] == status
    assert json.loads(metadata[0]['raw_response']) == body


@pytest.mark.parametrize('content', ['状态判断。' * 32, 'invalid Unicode: \ud800'])
def test_small_raw_response_is_retained_in_full_even_when_contract_is_invalid(server, capsys, content):
    from rc_llm_failure.errors import ProviderError
    from rc_llm_failure.runtime import FailurePolicy
    from llm_failure_helpers import Runtime, done
    body = response_for('deepseek', {'failed': False})
    body['choices'][0]['message']['content'] = content
    body['usage']['completion_tokens'] = 128
    server.responses.append({'body': body})
    instance, runtime = FailurePolicy(manifest(), provider_settings(base_url=server.url)), Runtime()
    chunk = [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        done(instance)
        assert instance.policy(chunk, {}, runtime) is chunk
        assert instance.monitor()['failed_requests'] == 1
    finally:
        instance.close()
    values = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    terminal = next(value for value in values if value['event'] == 'llm_failure.request.error')
    assert terminal['truncated'] is False
    assert json.loads(terminal['raw_response'])['choices'][0]['message']['content'] == content


def decision_settings(server=None, **patch):
    return {**provider_settings(profile='providers/openai_decisions.yaml.example'),
            'model': 'gpt-test', 'base_url': server.url if server else 'https://api.openai.com/v1',
            'api_key': 'decision-private-key', 'timeout_s': 2., **patch}


def decision_response(choice='failed'):
    return {'model': 'gpt-response', 'answers': [
        {'type': 'choice', 'name': 'failed', 'choice': choice, 'confidence': .9,
         'probabilities': [{'value': choice, 'probability': .9}]}],
        'usage': {'input_tokens': 10, 'output_tokens': 1, 'total_tokens': 11}}


@pytest.mark.parametrize('choice,expected', [('failed', True), ('not_failed', False)])
def test_decisions_real_sdk_preserves_history_and_diagnostics(server, monkeypatch, capsys, choice, expected):
    from rc_llm_failure.runtime import FailurePolicy
    from llm_failure_helpers import Runtime, done
    original = server.RequestHandlerClass.end_headers
    def headers(handler):
        handler.send_header('x-request-id', 'decision-request-123')
        original(handler)
    monkeypatch.setattr(server.RequestHandlerClass, 'end_headers', headers)
    server.responses.append({'body': decision_response(choice)})
    instance, runtime = FailurePolicy(manifest(), decision_settings(server)), Runtime()
    chunk = [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        done(instance)
        instance.policy(chunk, {}, runtime)
    finally:
        instance.close()
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
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


@pytest.mark.parametrize('choice', [True, 'unknown'])
def test_decisions_rejects_unoffered_choices_without_retry(server, choice):
    from rc_llm_failure.providers import ProviderClient
    from rc_llm_failure.errors import ProviderError
    server.responses.append({'body': decision_response(choice)})
    settings = decision_settings(server)
    provider = ProviderClient(settings)
    try:
        with pytest.raises(ProviderError, match='契约'):
            provider.decide(snapshots())
    finally:
        provider.close()
    assert len(server.requests) == 1


def test_only_selected_provider_file_needs_usable_credentials(tmp_path):
    from rc_llm_failure.configuration import load_provider
    (tmp_path / 'deepseek.yaml').write_text(json.dumps(provider_settings(api_key='')))
    (tmp_path / 'decisions.yaml').write_text(json.dumps(decision_settings()))
    assert load_provider('decisions.yaml', tmp_path)['type'] == 'openai_decisions'
    with pytest.raises(ValueError, match='api_key'):
        load_provider('deepseek.yaml', tmp_path)


@pytest.mark.parametrize('kind', ['http', 'timeout', 'refusal', 'wrong_name', 'extra_answer'])
def test_decisions_errors_keep_diagnostics_and_never_retry(server, kind):
    from rc_llm_failure.providers import ProviderClient
    from rc_llm_failure.errors import ProviderError
    body = decision_response()
    gate = threading.Event()
    item = {'body': body}
    if kind == 'http':
        item = {'status': 503, 'body': {'error': {'message': 'decision-private-key'}}}
    elif kind == 'timeout':
        item = {'wait': gate}
    elif kind == 'refusal':
        body['answers'] = [{'type': 'refusal', 'name': 'failed'}]
    elif kind == 'wrong_name':
        body['answers'][0]['name'] = 'other'
    else:
        body['answers'].append(body['answers'][0])
    server.responses.append(item)
    metadata = []
    settings = decision_settings(server, timeout_s=2.)
    provider = ProviderClient(settings, diagnostics=metadata.append)
    try:
        with pytest.raises(ProviderError) as error:
            provider.decide(snapshots())
    finally:
        gate.set()
        provider.close()
    if kind == 'timeout':
        assert server.received.is_set(), 'timeout must follow receipt by the mock HTTP server'
        assert 'API 请求超时' in str(error.value)
    assert 'decision-private-key' not in str(error.value) + json.dumps(metadata)
    assert len(server.requests) == len(metadata) == 1
    assert metadata[0]['provider'] == 'openai_decisions'
    assert metadata[0]['error'] and metadata[0]['http_duration_s'] >= 0


def test_decisions_runtime_keeps_backend_metadata_and_scrubs_profile_key(server, capsys):
    from rc_llm_failure.runtime import FailurePolicy
    from llm_failure_helpers import Runtime, done
    body = decision_response()
    body['usage']['input_tokens_details'] = {'cached_tokens': 8, 'cache_write_tokens': 0}
    body['usage']['output_tokens_details'] = {'reasoning_tokens': 1}
    body['extra'] = 'decision-private-key'
    server.responses.append({'body': body})
    instance, runtime = FailurePolicy(manifest(), decision_settings(server, timeout_s=7.)), Runtime()
    chunk = [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        done(instance)
        instance.policy(chunk, {}, runtime)
    finally:
        instance.close()
    text = capsys.readouterr().out
    assert 'decision-private-key' not in text
    events = [json.loads(line) for line in text.splitlines()]
    assert [event['event'] for event in events] == [
        'llm_failure.request.started', 'llm_failure.request.completed', 'llm_failure.collection.accepted']
    assert all(event['provider'] == 'openai_decisions' for event in events)
    assert events[1]['usage']['input_tokens_details']['cached_tokens'] == 8
    assert events[1]['usage']['output_tokens_details']['reasoning_tokens'] == 1


def test_decisions_reset_discards_late_true_and_recording_keeps_backend(server, tmp_path, capsys):
    from rc_llm_failure.runtime import FailurePolicy
    from llm_failure_helpers import Runtime, done
    from robot_control.recording_writer import SessionFiles
    server.responses.append({'body': decision_response()})
    instance = FailurePolicy(manifest(), decision_settings(server))
    runtime, chunk = Runtime(), [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        done(instance)
        instance.reset_context()
        instance.policy(chunk, images(), runtime)
        assert runtime.finishes == []
    finally:
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


@pytest.mark.parametrize('decision_api', [False, True])
def test_episode_monitor_uses_http_duration_and_actual_total_usage_only(server, monkeypatch, decision_api):
    import rc_llm_failure.runtime as runtime_module
    import rc_llm_failure.providers as providers_module
    from llm_failure_helpers import Runtime, done, wait
    import threading
    clock = [100.]
    monkeypatch.setattr(runtime_module, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(providers_module, 'monotonic', lambda: clock[0])
    settings = decision_settings(server, timeout_s=100.) if decision_api else provider_settings(base_url=server.url, timeout_s=100.)
    bodies = [decision_response('not_failed') if decision_api else response_for('deepseek', {'failed': False})
              for _ in range(4)]
    bodies[0]['usage']['total_tokens'] = 10
    bodies[1]['usage']['total_tokens'] = 30
    bodies[2].pop('usage')
    bodies[3]['usage'].pop('total_tokens')  # Never derive total from input/output fields.
    gates = [threading.Event() for _ in bodies]
    server.responses.extend({'wait': gate, 'body': body} for gate, body in zip(gates, bodies))
    original = providers_module.ProviderClient._capture_http
    def advance(self, metadata, response, request_id=None):
        original(self, metadata, response, request_id)
        # Parsing/collection delay must not become an HTTP duration sample.
        clock[0] += 7.
    monkeypatch.setattr(providers_module.ProviderClient, '_capture_http', advance)
    instance, runtime = runtime_module.FailurePolicy(manifest(), settings), Runtime()
    chunk = [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        for index, (step, duration) in enumerate(((60, 2.), (120, 4.), (180, 6.), (240, 8.))):
            runtime.consumed_actions = step
            instance.policy(chunk, images(), runtime)
            wait(lambda: len(server.requests) == index + 1)
            clock[0] += duration
            gates[index].set()
            done(instance)
            clock[0] += 11.
            instance.policy(chunk, {}, runtime)
        assert instance.monitor() == {'in_flight': 0, 'avg_http_s': 5., 'max_http_s': 8.,
                                      'avg_total_tokens': 20., 'max_total_tokens': 30, 'failed_requests': 0}
        instance.reset_context()
        assert instance.monitor()['avg_total_tokens'] == instance.monitor()['avg_http_s'] == '—'
    finally:
        for gate in gates:
            gate.set()
        instance.close()
    assert len(server.requests) == 4


@pytest.mark.parametrize('decision_api', [False, True])
@pytest.mark.parametrize('failure', ['http', 'disconnect', 'timeout', 'refusal', 'invalid'])
def test_real_supplier_failure_is_passthrough_then_next_normal_check_succeeds(server, decision_api, failure):
    from rc_llm_failure.runtime import FailurePolicy
    from llm_failure_helpers import Runtime, done
    body = decision_response('not_failed') if decision_api else response_for('deepseek', {'failed': False})
    if failure == 'http':
        item = {'status': 503, 'body': {'error': {'message': 'unavailable'}}}
    elif failure == 'disconnect':
        item = {'disconnect': True}
    elif failure == 'timeout':
        item = {'delay': 1.}
    else:
        if decision_api:
            body['answers'] = ([{'type': 'refusal', 'name': 'failed'}] if failure == 'refusal' else
                               [{'type': 'choice', 'name': 'failed', 'choice': 'unknown'}])
        else:
            body['choices'][0]['message']['refusal' if failure == 'refusal' else 'content'] = 'invalid'
        item = {'body': body}
    success = decision_response('failed') if decision_api else response_for('deepseek', {'failed': True})
    server.responses.extend([item, {'body': success}])
    timeout = .5 if failure == 'timeout' else 2.
    settings = decision_settings(server, timeout_s=timeout) if decision_api else provider_settings(base_url=server.url, timeout_s=timeout)
    instance, runtime = FailurePolicy(manifest(), settings), Runtime()
    chunk = [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        done(instance)
        assert instance.policy(chunk, {}, runtime) is chunk
        assert instance.monitor()['failed_requests'] == 1
        assert runtime.finishes == []
        runtime.consumed_actions = 119
        instance.policy(chunk, {}, runtime)
        assert len(server.requests) == 1
        runtime.consumed_actions = 120
        instance.policy(chunk, images(), runtime)
        done(instance)
        instance.policy(chunk, {}, runtime)
        assert len(runtime.finishes) == 1
        assert instance.monitor()['failed_requests'] == 1
        assert len(server.requests) == 2
    finally:
        instance.close()


@pytest.mark.parametrize('decision_api', [False, True])
def test_request_budget_and_request_construction_errors_remain_local_faults(server, monkeypatch, decision_api):
    from rc_llm_failure.errors import ProviderError
    from rc_llm_failure.providers import ProviderClient
    import rc_llm_failure.providers as module
    provider = ProviderClient(decision_settings(server) if decision_api else provider_settings(base_url=server.url))
    try:
        monkeypatch.setattr(module, 'MAX_REQUEST_BYTES', 1)
        with pytest.raises(RuntimeError, match='32MiB') as error:
            provider.decide(snapshots())
        assert not isinstance(error.value, ProviderError)
        monkeypatch.setattr(module, 'MAX_REQUEST_BYTES', 32 * 1024 * 1024)
        with pytest.raises(TypeError):
            provider.decide(None)
        assert not server.requests
    finally:
        provider.close()


@pytest.mark.parametrize('decision_api', [False, True])
def test_multiple_real_supplier_failures_then_success_do_not_reinitialize_or_retry(server, decision_api):
    from rc_llm_failure.runtime import FailurePolicy
    from llm_failure_helpers import Runtime, done
    success = decision_response('failed') if decision_api else response_for('deepseek', {'failed': True})
    server.responses.extend([{'status': 503, 'body': {'error': {'message': 'unavailable'}}},
                             {'disconnect': True}, {'body': success}])
    settings = decision_settings(server) if decision_api else provider_settings(base_url=server.url)
    instance, runtime = FailurePolicy(manifest(), settings), Runtime()
    chunk = [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        for step in (60, 120, 180):
            runtime.consumed_actions = step
            instance.policy(chunk, images(), runtime)
            done(instance)
            instance.policy(chunk, {}, runtime)
            assert instance.monitor()['failed_requests'] == min(step // 60, 2)
            assert bool(runtime.finishes) is (step == 180)
            assert len(server.requests) == step // 60
    finally:
        instance.close()


@pytest.mark.parametrize('decision_api', [False, True])
@pytest.mark.parametrize('invalidate', ['timeout', 'reset'])
def test_real_http_old_completion_cannot_finish_or_pollute_new_episode(server, monkeypatch, decision_api, invalidate):
    import threading
    import rc_llm_failure.runtime as module
    from llm_failure_helpers import Runtime, done, wait
    clock, gate = [100.], threading.Event()
    monkeypatch.setattr(module, 'monotonic', lambda: clock[0])
    body = decision_response('failed') if decision_api else response_for('deepseek', {'failed': True})
    server.responses.append({'wait': gate, 'body': body})
    settings = decision_settings(server, timeout_s=10.) if decision_api else provider_settings(base_url=server.url, timeout_s=10.)
    instance, runtime = module.FailurePolicy(manifest(), settings), Runtime()
    chunk, resetter = [[0.] * 14], None
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        assert server.received.wait(2)
        assert instance.monitor()['in_flight'] == 1
        if invalidate == 'timeout':
            clock[0] = 111.
            for _ in range(3):
                assert instance.policy(chunk, {}, runtime) is chunk
            assert instance.monitor()['failed_requests'] == 1
        else:
            resetter = threading.Thread(target=instance.reset_context)
            resetter.start()
            wait(lambda: instance._generation == 1)
        assert instance.monitor()['in_flight'] == 1
        gate.set()
        if resetter:
            resetter.join(2)
            assert not resetter.is_alive()
        else:
            done(instance)
        assert instance.monitor()['in_flight'] == 0
        assert instance.policy(chunk, images(), runtime) is chunk
        assert runtime.finishes == []
        if invalidate == 'reset':
            assert instance.monitor()['failed_requests'] == 0
            assert instance.monitor()['avg_http_s'] == instance.monitor()['avg_total_tokens'] == '—'
        else:
            assert instance.monitor()['failed_requests'] == 1
            assert instance.monitor()['max_total_tokens'] == (11 if decision_api else 20)
        assert len(server.requests) == 1
    finally:
        gate.set()
        if resetter:
            resetter.join(2)
        instance.close()


@pytest.mark.parametrize('decision_api', [False, True])
def test_oversized_runtime_query_fault_keeps_actionable_diagnostic_without_supplier_sample(server, monkeypatch, decision_api):
    import rc_llm_failure.providers as providers
    from rc_llm_failure.runtime import FailurePolicy
    from llm_failure_helpers import Runtime, done
    monkeypatch.setattr(providers, 'MAX_REQUEST_BYTES', 1)
    settings = decision_settings(server) if decision_api else provider_settings(base_url=server.url)
    instance, runtime = FailurePolicy(manifest(), settings), Runtime()
    chunk = [[0.] * 14]
    try:
        instance.policy(chunk, images(), runtime)
        runtime.consumed_actions = 60
        instance.policy(chunk, images(), runtime)
        done(instance)
        with pytest.raises(RuntimeError, match='32MiB'):
            instance.policy(chunk, {}, runtime)
        assert instance.monitor()['failed_requests'] == 0
        assert instance.monitor()['avg_http_s'] == instance.monitor()['avg_total_tokens'] == '—'
        assert not server.requests
    finally:
        instance.close()
