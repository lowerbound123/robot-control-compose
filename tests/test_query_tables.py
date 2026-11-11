"""Builtin adapters preserve complete readonly snapshots and original results."""
import copy
import json

import pytest

from robot_control.table_output import public_table_fields


def adapt(kind, result):
    from robot_control.query_tables import query_table_document
    document = query_table_document(kind, result)
    assert document is not None
    return public_table_fields(result, document)


def values(event):
    return {row[0]: row[-1] for table in event['table_document']['tables'] for row in table['rows']}


def test_status_flattens_every_future_field_with_unambiguous_paths_and_empty_containers():
    result = {'state': 'paused', 'runtime': {'future': {'enabled': False, 'count': 0, 'null': None,
        'empty': '', 'object': {}, 'list': [], 'values': [{'a': 1}]}},
        'a.b': {'x': 7}, 'a': {'b.x': 8}, 'unknown': {'deep': {'value': 'complete'}}}
    original = copy.deepcopy(result)
    event = adapt('status', result)
    projected = values(event)
    assert projected['runtime.future.enabled'] is False
    assert projected['runtime.future.count'] == 0
    assert projected['runtime.future.null'] is None
    assert projected['runtime.future.empty'] == ''
    assert projected['runtime.future.object'] == {}
    assert projected['runtime.future.list'] == []
    assert projected['runtime.future.values'] == [{'a': 1}]
    assert projected['["a.b"].x'] == 7 and projected['a["b.x"]'] == 8
    assert projected['unknown.deep.value'] == 'complete'
    assert json.loads(event['copy_json']) == result == original


def test_connector_instances_are_independent_and_unknown_fields_are_retained():
    result = {'phase': 'ready', 'new': 0, 'instances': {
        'same-0': {'source': 'same', 'status': 'succeeded', 'extra': {'flag': False}},
        'same-1': {'source': 'same', 'status': 'failed', 'error': '/full/' + 'long/' * 100, 'future': []}}}
    event = adapt('connector status', result)
    text = event['message']
    for token in ('same-0', 'same-1', 'extra.flag', 'future', '[]', result['instances']['same-1']['error']):
        assert token in text
    assert json.loads(event['copy_json']) == result


@pytest.mark.parametrize('instances', [{}, []])
def test_empty_connector_instances_remain_explicit_json_values(instances):
    result = {'phase': 'ready', 'instances': instances}
    event = adapt('connector status', result)
    assert values(event)['instances'] == instances
    assert '无记录' in event['message']


def test_connector_list_keeps_false_missing_values_and_complete_errors():
    error = '[red] /broken/' + 'x' * 200
    result = [{'name': 'ok', 'source': 'mock', 'connector': 'body', 'valid': True, 'config': '/long/config'},
              {'name': 'bad', 'valid': False, 'error': error}]
    event = adapt('connector list', result)
    table = event['table_document']['tables'][0]
    assert table['columns'] == ['名称', '来源', '实例', '有效', '配置路径', '错误']
    assert table['rows'][1] == ['bad', '—', '—', False, '—', error]
    assert json.loads(event['copy_json']) == result
    empty = adapt('connector list', [])
    assert '无记录' in empty['message'] and json.loads(empty['copy_json']) == []


def test_policy_catalog_and_recording_preserve_details_and_future_statistics():
    policies = [{'id': 'chain', 'kind': 'chain', 'state': 'available', 'control_mode': None,
                 'valid': True, 'directory': '/complete/path', 'future': {'count': 0}},
                {'id': 'invalid', 'valid': False, 'error': '完整错误'}]
    event = adapt('policies', policies)
    assert json.loads(event['copy_json']) == policies
    assert all(value in event['message'] for value in ('directory', '/complete/path', 'future', '完整错误'))
    recording = {'state': 'idle', 'session_id': None, 'path': '', 'error': None,
        'pending_samples': 0, 'pending_bytes': 0, 'future': {'kept': False}, 'stats': {}}
    event = adapt('record status', recording)
    assert values(event)['future.kept'] is False and values(event)['stats'] == {}
    assert json.loads(event['copy_json']) == recording


def test_unrelated_queries_stay_unadapted_and_secret_paths_use_public_boundary():
    from robot_control.query_tables import query_table_document
    payload = {'table_document': {}, 'api_key': 123, 'x': {'api_key': 'SECRET'}}
    assert query_table_document('show rc', payload) is None
    event = adapt('status', payload)
    assert 'SECRET' not in event['message'] and '123' not in event['message']
    assert json.loads(event['copy_json'])['api_key'] == '***'


_SENSITIVE_OBJECT = {'x-y': 123456, 'quoted.key': False, 'empty.object': {}, 'empty-list': []}


@pytest.mark.parametrize('kind,payload,expected', [
    ('status', {'api_key': _SENSITIVE_OBJECT, 'visible': 'kept'},
     {'api_key': '***', 'visible': 'kept'}),
    ('status', {'runtime': {'api_key': _SENSITIVE_OBJECT, 'visible': 'kept'}},
     {'runtime': {'api_key': '***', 'visible': 'kept'}}),
    ('record status', {'state': 'idle', 'future': {'api_key': _SENSITIVE_OBJECT, 'visible': 'kept'}},
     {'state': 'idle', 'future': {'api_key': '***', 'visible': 'kept'}}),
    ('connector status', {'instances': {'same-0': {'future': {'api_key': _SENSITIVE_OBJECT}},
                                      'same-1': {'visible': 'kept'}}},
     {'instances': {'same-0': {'future': {'api_key': '***'}}, 'same-1': {'visible': 'kept'}}}),
    ('connector list', [{'name': 'main', 'valid': True, 'future': {'api_key': _SENSITIVE_OBJECT}, 'visible': 'kept'}],
     [{'name': 'main', 'valid': True, 'future': {'api_key': '***'}, 'visible': 'kept'}]),
    ('policies', [{'id': 'example', 'valid': True, 'future': {'api_key': _SENSITIVE_OBJECT}, 'visible': 'kept'}],
     [{'id': 'example', 'valid': True, 'future': {'api_key': '***'}, 'visible': 'kept'}]),
])
def test_sensitive_objects_are_redacted_before_special_key_projection(kind, payload, expected):
    from robot_control.query_tables import query_table_document
    original = copy.deepcopy(payload)
    document = query_table_document(kind, payload)
    projected = json.dumps(document, ensure_ascii=False)
    # Sensitive child keys are absent, not merely given redacted leaf values.
    # This covers numeric, boolean and empty-container contents under brackets.
    for hidden in ('123456', 'x-y', 'quoted.key', 'empty.object', 'empty-list'):
        assert hidden not in projected
    assert '***' in projected and 'kept' in projected
    fields = public_table_fields(payload, document)
    assert json.loads(fields['copy_json']) == expected
    assert payload == original


def test_query_projection_owns_its_public_snapshot_without_mutating_business_values():
    from robot_control.query_tables import query_table_document
    payload = {'future': {'values': [0, {'safe': 'kept'}], 'api_key': _SENSITIVE_OBJECT}}
    original = copy.deepcopy(payload)
    document = query_table_document('status', payload)
    row = next(row for table in document['tables'] for row in table['rows'] if row[0] == 'future.values')
    row[1][1]['safe'] = 'changed only in projection'
    assert payload == original


@pytest.mark.parametrize('command', ['show rc', 'show policy', 'show global', 'mcp status'])
def test_unselected_queries_return_before_inspecting_or_copying_business_result(command):
    from robot_control.query_tables import query_table_document
    class UninspectedValue:
        def __deepcopy__(self, memo):
            raise AssertionError('unselected query must not construct a public snapshot')
    assert query_table_document(command, {'value': UninspectedValue()}) is None
