"""Check the failure policy's provider-specific profile, wire and verdict contracts."""
import json

import pytest

pytest.importorskip('openai', reason='Decisions checks require the optional LLM environment')
from llm_failure_helpers import images, manifest, provider_settings
from llm_http_helpers import model_server
from rc_llm_failure.configuration import load_provider
from rc_llm_failure.errors import ProviderError
from rc_llm_failure.images import encode_snapshot
from rc_llm_failure.providers import ProviderClient


PROFILES = {
    'predicate': {'fail_if': {'op': '>=', 'threshold': .8}},
    'choice': {'choices': [{'value': True, 'description': 'moving', 'failed': False},
                           {'value': 'true', 'failed': True}, {'value': False, 'failed': True}]},
    'score': {'levels': [{'label': 'normal'}, {'label': 'deviating', 'description': 'off target'},
                         {'label': 'stalled'}], 'fail_if': {'op': '<=', 'threshold': 1.5}},
}


def profile(kind, **changes):
    return {**provider_settings(type='gpt-decision-' + kind, question_name='outcome',
                                prompt='Check the images.', response_instructions=''),
            **PROFILES[kind], **changes}


def load(tmp_path, settings):
    path = tmp_path / 'candidate.yaml'
    path.write_text(json.dumps(settings))
    return load_provider(path.name, tmp_path)


@pytest.mark.parametrize('type_field', [{}, {'type': None}, {'type': True}, {'type': ['deepseek']},
                                        {'type': 'unknown'}], ids=['missing', 'null', 'boolean', 'list', 'unknown'])
def test_preflight_and_direct_client_require_explicit_provider_type(tmp_path, type_field):
    settings = provider_settings()
    del settings['type']
    settings.update(type_field)
    with pytest.raises(ValueError, match='provider type'):
        load(tmp_path, settings)
    with pytest.raises(ValueError, match='provider type'):
        ProviderClient(settings).close()


@pytest.mark.parametrize('count', [2, 255])
def test_choice_profile_accepts_native_option_count_limits(tmp_path, count):
    choices = [{'value': str(n), 'failed': n == 0} for n in range(count)]
    assert load(tmp_path, profile('choice', choices=choices))['choices'] == choices


@pytest.mark.parametrize('count', [2, 10])
@pytest.mark.parametrize('upper_bound', [False, True])
def test_score_profile_threshold_can_equal_either_end(tmp_path, count, upper_bound):
    levels = [{'label': str(n)} for n in range(count)]
    rule = {'op': '<=', 'threshold': count - 1 if upper_bound else 0}
    settings = load(tmp_path, profile('score', levels=levels, fail_if=rule))
    assert settings['levels'] == levels and settings['fail_if'] == rule


@pytest.mark.parametrize('kind,changes', [
    ('predicate', {'question_name': '  '}),
    ('predicate', {'choices': []}),
    ('predicate', {'fail_if': {'op': '>', 'threshold': .5}}),
    ('predicate', {'fail_if': {'op': [], 'threshold': .5}}),
    ('predicate', {'fail_if': {'op': '<='}}),
    ('predicate', {'fail_if': {'op': '>=', 'threshold': 0, 'extra': 1}}),
    ('predicate', {'fail_if': {'op': '>=', 'threshold': True}}),
    ('predicate', {'fail_if': {'op': '<=', 'threshold': 1.01}}),
    ('score', {'fail_if': {'op': '<=', 'threshold': -0.01}}),
    ('score', {'fail_if': {'op': '>=', 'threshold': 2.01}}),
    ('score', {'fail_if': {'op': '>=', 'threshold': '1.5'}}),
    ('score', {'fail_if': {'op': '>=', 'threshold': float('inf')}}),
    ('score', {'levels': ['normal', 'stalled']}),
    ('score', {'levels': [{'label': ''}, {'label': 'ok'}]}),
    ('score', {'levels': [{'label': str(n)} for n in range(11)]}),
    ('score', {'levels': [{'label': 'only'}]}),
    ('score', {'levels': [{'label': 'a'}, {'label': 'b', 'description': None}]}),
    ('score', {'levels': [{'label': 'a'}, {'label': 'b', 'failed': True}]}),
    ('choice', {'fail_if': {'op': '>=', 'threshold': .5}}),
    ('choice', {'choices': [{'value': 'only', 'failed': True}]}),
    ('choice', {'choices': [{'value': str(n), 'failed': False} for n in range(256)]}),
    ('choice', {'choices': [{'value': 'repeat', 'failed': False}, {'value': 'repeat', 'failed': True}]}),
    ('choice', {'choices': [{'value': True, 'failed': False}, {'value': True, 'failed': True}]}),
    ('choice', {'choices': [{'value': 1, 'failed': True}, {'value': False, 'failed': False}]}),
    ('choice', {'choices': [{'value': 'a', 'failed': 'false'}, {'value': 'b', 'failed': True}]}),
    ('choice', {'choices': [{'value': 'a'}, {'value': 'b', 'failed': True}]}),
    ('choice', {'choices': [{'value': 'a', 'failed': False, 'description': []}, {'value': 'b', 'failed': True}]}),
])
def test_invalid_question_and_local_rule_are_rejected_at_load(tmp_path, kind, changes):
    settings = profile(kind, **changes)
    if 'levels' in changes:
        # Keep the threshold valid even for a two-level candidate so the
        # malformed level itself must cause the rejection.
        settings['fail_if'] = {'op': '<=', 'threshold': 0}
    with pytest.raises(ValueError):
        load(tmp_path, settings)


@pytest.fixture
def server():
    with model_server() as value:
        yield value


@pytest.fixture
def history():
    return tuple(encode_snapshot(images(), step, manifest()['params']) for step in (0, 60))


def response(answer):
    return {'model': 'gpt-answer', 'answers': [answer],
            'usage': {'input_tokens': 13, 'output_tokens': 1, 'total_tokens': 14}}


def answer(kind, value):
    result = {'type': kind, 'name': 'outcome'}
    if kind == 'predicate':
        return {**result, 'probability': value}
    if kind == 'choice':
        return {**result, 'choice': value, 'confidence': .9,
                'probabilities': [{'value': value, 'probability': .9}]}
    return {**result, 'score': value, 'confidence': .9,
            'probabilities': [{'label': 'normal', 'value': 0, 'probability': .9}]}


@pytest.mark.parametrize('kind,options,values', [
    ('predicate', {}, [(0, False), (.799, False), (.8, True), (1, True)]),
    ('choice', {'choices': [{'value': True, 'description': 'moving'}, {'value': 'true'}, {'value': False}]},
     [(True, False), ('true', True), (False, True)]),
    ('score', {'levels': [{'label': 'normal'}, {'label': 'deviating', 'description': 'off target'},
                          {'label': 'stalled'}]}, [(0, True), (1.5, True), (1.501, False), (2, False)]),
])
def test_native_question_and_local_mapping_over_http(tmp_path, server, history, kind, options, values):
    settings = load(tmp_path, profile(kind, base_url=server.url))
    metadata = []
    provider = ProviderClient(settings, diagnostics=metadata.append)
    try:
        for value, expected in values:
            server.responses.append({'body': response(answer(kind, value))})
            assert provider.decide(history) is expected
    finally:
        provider.close()
    assert len(server.requests) == len(metadata) == len(values)
    for request, event in zip(server.requests, metadata):
        assert request['path'] == '/v1/decisions'
        assert set(request['body']) == {'model', 'input', 'questions'}
        assert request['body']['questions'] == [
            {'type': kind, 'name': 'outcome', 'instructions': 'Check the images.', **options}]
        assert event['provider'] == 'gpt-decision-' + kind
        assert event['finish_reason'] == kind and event['usage']['total_tokens'] == 14


@pytest.mark.parametrize('kind,rule,values', [
    ('predicate', {'op': '<=', 'threshold': 0}, [(0, True), (.01, False)]),
    ('score', {'op': '>=', 'threshold': 2}, [(1.99, False), (2, True)]),
])
def test_failure_direction_can_be_reversed_per_profile(server, history, kind, rule, values):
    provider = ProviderClient(profile(kind, base_url=server.url, fail_if=rule))
    try:
        for value, expected in values:
            server.responses.append({'body': response(answer(kind, value))})
            assert provider.decide(history) is expected
    finally:
        provider.close()


@pytest.mark.parametrize('kind,field,bad_values', [
    ('predicate', 'probability', [True, False, '.8', None, -.01, 1.01, float('nan'), float('inf')]),
    ('score', 'score', [True, False, '1.5', None, -.01, 2.01, float('nan'), -float('inf')]),
    ('choice', 'choice', [0, 1, None, 'unoffered', ['true']]),
])
def test_raw_wire_types_and_single_named_answer_are_required(server, history, kind, field, bad_values):
    # All three questions use valid local profiles. Every bad verdict must be a
    # recoverable supplier error, including booleans coerced into floats by the SDK.
    provider = ProviderClient(profile(kind, base_url=server.url))
    valid = answer(kind, True if kind == 'choice' else .8)
    malformed = [[{**valid, field: value}] for value in bad_values]
    other_type = 'choice' if kind != 'choice' else 'score'
    malformed += [[{key: value for key, value in valid.items() if key != field}],
                  [{**valid, 'name': 'wrong'}], [{**valid, 'type': other_type}], [valid, valid],
                  [], [None], [{'name': 'outcome', 'type': 'refusal'}]]
    try:
        for answers in malformed:
            server.responses.append({'body': {**response(valid), 'answers': answers}})
            with pytest.raises(ProviderError):
                provider.decide(history)
    finally:
        provider.close()
    assert [request['path'] for request in server.requests] == ['/v1/decisions'] * len(malformed)


def test_nonstandard_json_constants_in_unused_fields_invalidate_the_verdict(server, history):
    provider = ProviderClient(profile('choice', base_url=server.url))
    try:
        for field, value in (('confidence', float('nan')), ('extra', [float('inf')]),
                             ('extra', {'nested': -float('inf')})):
            # The fixture's HTTP JSON encoder writes bare NaN, Infinity or -Infinity.
            choice = {**answer('choice', 'true'), field: value}
            server.responses.append({'body': response(choice)})
            with pytest.raises(ProviderError, match='契约'):
                provider.decide(history)
        server.responses.append({'body': response({**answer('choice', 'true'), 'extra': 'Infinity'})})
        assert provider.decide(history) is True
    finally:
        provider.close()
    assert len(server.requests) == 4
