"""Table projections obey recorder redaction and survive the real writer process."""
import copy
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from rc_policy_sdk import Table, TableOutput
from rc_policy_sdk.outbox import PriorityOutbox
from rc_policy_sdk.runtime import PolicyRuntime
from rc_policy_sdk.tables import normalize_table_output
from rc_policy_sdk.wire import MAX_MESSAGE_BYTES, encode_message
from robot_control.recording import RecordingService
from robot_control.recording_writer import clean, encoded_line
from robot_control.table_output import public_json_fields, public_table_fields


def fields(result, *tables):
    return public_table_fields(*normalize_table_output(TableOutput(result=result, tables=list(tables))))


def persisted(value, secrets=()):
    return json.loads(encoded_line(value, secrets))


@pytest.mark.parametrize('key', ['password', 'Authorization', 'proxy-authorization',
                                 'client_secret', 'access_token', 'access-token', 'apiKey'])
def test_sensitive_business_fields_are_hidden_in_every_table_projection(key):
    secret = 'private-business-value'
    raw = {key: {'parts': [secret]}, 'safe': 0}
    value = fields(raw, Table(title='echo ' + secret, columns=['echo ' + secret, 'value'],
                              rows=[['echo ' + secret, secret], ['keep', False]]))
    before = copy.deepcopy(value)
    plain = persisted({'message': json.dumps(raw)})
    assert json.loads(plain['message']) == {key: '***', 'safe': 0}
    saved = persisted(value)
    assert secret not in json.dumps(saved)
    assert json.loads(saved['copy_json']) == {key: '***', 'safe': 0}
    assert saved['table_document']['tables'][0] == {
        'title': 'echo ***', 'columns': ['echo ***', 'value'],
        'rows': [['echo ***', '***'], ['keep', False]]}
    assert saved['message'] == 'echo ***\necho ***\tvalue\necho ***\t***\nkeep\tfalse'
    assert value == before


def test_sensitive_columns_hide_values_and_their_echoes_without_a_sensitive_result_key():
    value = fields({'echo': 'column-private'}, Table(
        title='column-private', columns=['params.password', 'secret', 'authorization', 'access_token', 'safe'],
        rows=[['column-private', {'nested': ['nested-private']}, 'Bearer header-private', 0, 'nested-private'],
              [None, False, '', [], False]]))
    saved = persisted(value)
    assert json.loads(saved['copy_json']) == {'echo': '***'}
    assert saved['table_document']['tables'][0]['rows'] == [
        ['***', '***', '***', '***', '***'], ['***', '***', '***', '***', False]]
    assert saved['message'] == ('***\nparams.password\tsecret\tauthorization\taccess_token\tsafe\n'
                                '***\t***\t***\t***\t***\n***\t***\t***\t***\tfalse')


@pytest.mark.parametrize('label,value_column', [('field', 'value'), ('path', 'value_text'),
                                              ('key', 'value'), ('name', 'value'), ('字段', '值')])
def test_sensitive_field_paths_hide_the_associated_value_and_echo(label, value_column):
    value = fields({'safe': True}, Table(columns=[label, value_column, 'echo'], rows=[
        ['params.credentials.access-token', {'nested': ['path-private']}, 'path-private'],
        ['params.counter', 0, 'keep'], ['params.empty', '', None]]))
    saved = persisted(value)
    assert saved['table_document']['tables'][0]['rows'] == [
        ['params.credentials.access-token', '***', '***'],
        ['params.counter', 0, 'keep'], ['params.empty', '', None]]
    assert 'path-private' not in json.dumps(saved)
    assert json.loads(saved['copy_json']) == {'safe': True}


def test_nested_json_multitables_and_configured_credential_variants_remain_parseable():
    secret = 'known-密钥-"quote"-\\path'
    variants = list(dict.fromkeys((secret, json.dumps(secret)[1:-1],
                                  json.dumps(secret, ensure_ascii=False)[1:-1])))
    embedded = json.dumps({'outer': json.dumps({'password': 'nested-private', 'keep': [0, False, None, '']})})
    raw = {'body': embedded, 'known': variants, 'patch': {'path': 'auth.password', 'value': 'patch-private'}}
    value = fields(raw,
        Table(title='nested-private', columns=['body', 'known'], rows=[[embedded, variants]]),
        Table(title='patch-private', columns=['zero', 'false', 'null', 'empty'], rows=[[0, False, None, '']]),
        Table(title=secret, columns=['empty'], rows=[]))
    before = copy.deepcopy(value)
    saved = persisted(value, (secret,))
    copied = json.loads(saved['copy_json'])
    assert json.loads(json.loads(copied['body'])['outer']) == {'password': '***', 'keep': [0, False, None, '']}
    assert copied['known'] == ['***'] * len(variants)
    assert copied['patch'] == {'path': 'auth.password', 'value': '***'}
    tables = saved['table_document']['tables']
    assert tables[0]['title'] == '***'
    assert json.loads(json.loads(tables[0]['rows'][0][0])['outer'])['password'] == '***'
    assert tables[0]['rows'][0][1] == ['***'] * len(variants)
    assert tables[1] == {'title': '***', 'columns': ['zero', 'false', 'null', 'empty'], 'rows': [[0, False, None, '']]}
    assert tables[2] == {'title': '***', 'columns': ['empty'], 'rows': []}
    assert '\nzero\tfalse\tnull\tempty\n0\tfalse\tnull\t\n\n***\nempty\n无记录' in saved['message']
    assert 'nested-private' not in json.dumps(saved) and 'patch-private' not in json.dumps(saved)
    assert value == before


@pytest.mark.parametrize('document', [{'schema_version': 99},
    {'schema_version': 1, 'tables': [{'title': 'invalid-private', 'columns': ['value'], 'rows': [[], ['invalid-private']]}]}])
def test_invalid_display_falls_back_to_redacted_business_json(document):
    value = {'message': 'password\tinvalid-private', 'table_document': document,
             'copy_json': '{"password":"invalid-private","echo":"invalid-private","keep":false}'}
    before = copy.deepcopy(value)
    saved = persisted(value)
    assert 'table_document' not in saved
    assert saved['message'] == saved['copy_json']
    assert json.loads(saved['message']) == {'password': '***', 'echo': '***', 'keep': False}
    assert value == before


def test_invalid_copy_uses_plain_fallback_with_sensitive_table_values_hidden():
    value = fields({'safe': True}, Table(columns=['password'], rows=[['invalid-copy-private']]))
    value['copy_json'] = 'invalid JSON echo invalid-copy-private'
    before = copy.deepcopy(value)
    saved = persisted(value)
    assert 'table_document' not in saved
    assert saved['message'] == saved['copy_json'] == 'invalid JSON echo ***'
    assert value == before


def test_ordinary_same_named_business_dicts_are_not_table_events():
    business = {'table_document': {'schema_version': 1, 'tables': [
        {'title': 'ordinary', 'columns': ['password'], 'rows': [['business-value']]}]},
        'message': 'original business message', 'copy_json': '{"business": true}', 'tables': False}
    assert clean(business) == business
    assert persisted({'result': business}) == {'result': business}
    output = public_json_fields(business)
    saved = persisted(output)
    assert saved == output
    assert json.loads(saved['message']) == business


@pytest.mark.parametrize('array', [False, True], ids=['objects', 'arrays'])
def test_thousand_shallow_json_cells_preserve_all_three_copies_and_fit_sdk_budget(array):
    raw = [[number, {'keep': False}] if array else {'x': number} for number in range(1200)]
    output = TableOutput(result=raw, tables=[Table(columns=['value'], rows=[[row] for row in raw])])
    messages = []
    runtime = PolicyRuntime(SimpleNamespace(context_id='c', config_version=1, table_output_v1=True,
        send_log=lambda message: messages.append(message) or True), {'context_id': 'c', 'config_version': 1})
    assert runtime.log_table(output)
    size = len(encode_message(messages[0]))
    assert size < PriorityOutbox().log_maxbytes < MAX_MESSAGE_BYTES
    value = public_table_fields(*normalize_table_output(output))
    ordinary = persisted({'message': json.dumps(raw)})
    assert json.loads(ordinary['message']) == raw
    before = copy.deepcopy(value)
    saved = persisted(value)
    assert saved['table_document'] == value['table_document']
    assert saved['message'] == 'value\n' + '\n'.join(json.dumps(row, separators=(',', ':')) for row in raw)
    assert json.loads(saved['copy_json']) == raw
    assert value == before


def test_plain_text_with_thousand_json_fragments_is_iterative_and_preserves_boundaries():
    original = 'prefix password=outside-private\n' + '\n'.join(
        json.dumps({'number': number, 'password': 'fragment-private', 'body': json.dumps({'secret': 'nested-private'})})
        for number in range(1200)) + '\nsuffix Authorization: Bearer tail-private'
    saved = clean(original)
    lines = saved.splitlines()
    assert lines[0] == 'prefix password=***'
    assert lines[-1] == 'suffix Authorization: ***'
    assert len(lines) == 1202
    for number, line in enumerate(lines[1:-1]):
        record = json.loads(line)
        assert record == {'number': number, 'password': '***', 'body': '{"secret":"***"}'}


def test_many_hidden_business_strings_use_bounded_matching_without_truncation():
    # The deadline covers serialization and all projections in a separate real
    # interpreter; per-secret scans of every large cell do not fit this budget.
    script = '''
import json
from rc_policy_sdk import Table, TableOutput
from rc_policy_sdk.tables import normalize_table_output
from robot_control.recording_writer import encoded_line
from robot_control.table_output import public_table_fields
raw = [{'password': f'PRIVATE_{number:08d}'} for number in range(5000)]
body = 'x' * 400000 + ' END'
fields = public_table_fields(*normalize_table_output(TableOutput(result=raw,
    tables=[Table(title='PRIVATE_00000042', columns=['text'], rows=[[body]])])))
for _ in range(3):
    saved = json.loads(encoded_line(fields))
    assert json.loads(saved['copy_json']) == [{'password': '***'}] * 5000
    assert saved['table_document']['tables'] == [{'title': '***', 'columns': ['text'], 'rows': [[body]]}]
    assert saved['message'] == '***\\ntext\\n' + body
'''
    result = subprocess.run([sys.executable, '-B', '-c', script], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('secret,literal', [
    ("a'\"b\\c", repr("a'\"b\\c")),
    ("控制\n\t\\'\"", ascii("控制\n\t\\'\"")),
    ('ABC', r"'\x41\u0042\103'"),
])
def test_quoted_credential_escapes_hide_decoded_and_encoded_echoes(secret, literal):
    body = 'password=' + literal
    value = fields({'body': body, 'echo': secret}, Table(
        title=secret, columns=['echo ' + secret], rows=[[secret], [literal[1:-1]]]))
    before = copy.deepcopy(value)
    assert clean(body) == 'password=***'
    saved = persisted(value)
    assert json.loads(saved['copy_json']) == {'body': 'password=***', 'echo': '***'}
    assert saved['table_document']['tables'] == [
        {'title': '***', 'columns': ['echo ***'], 'rows': [['***'], ['***']]}]
    assert saved['message'] == '***\necho ***\n***\n***'
    assert value == before


@pytest.mark.parametrize('prefix', ['[' * 1100, '{' * 1100, '[{' * 1100], ids=['array', 'object', 'mixed'])
@pytest.mark.parametrize('suffix', ['', '{"path":"params.password","value":"suffix-private","safe":[0,false]}'],
                         ids=['plain', 'sensitive-suffix'])
def test_literal_brackets_are_preserved_with_sensitive_json_suffix(prefix, suffix):
    text = prefix + suffix
    expected = prefix + ('{"path":"params.password","value":"***","safe":[0,false]}' if suffix else '')
    assert persisted({'message': text}) == {'message': expected}
    value = fields(text, Table(columns=['text'], rows=[[text]]))
    before = copy.deepcopy(value)
    saved = persisted(value)
    assert json.loads(saved['copy_json']) == expected
    assert saved['table_document']['tables'][0]['rows'] == [[expected]]
    assert saved['message'] == 'text\n' + expected
    assert value == before


def test_unmatched_and_nested_literal_brackets_have_bounded_cost():
    script = '''
from robot_control.recording_writer import clean
for prefix, suffix in [
    ('[' * 50000, ''), ('[{' * 25000, ''),
    ('[' * 50000, '{"path":"params.password","value":"HIDDEN"}'),
    ('[' * 10000, '{"password":"HIDDEN"}' + ']' * 10000),
]:
    text = prefix + suffix
    saved = clean(text)
    assert saved == prefix + suffix.replace('HIDDEN', '***')
'''
    result = subprocess.run([sys.executable, '-B', '-c', script], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('invalid', [
    'NaN', 'Infinity', '-Infinity', '1e999', '[0, NaN]',
    '{"outer":[{"number":1e999}],"password":"fallback-private"}',
])
def test_nonfinite_copy_falls_back_to_safe_literal_text(invalid):
    value = fields({'value': 0}, Table(columns=['value'], rows=[[0]]))
    value['copy_json'] = invalid
    before = copy.deepcopy(value)
    saved = persisted(value)
    assert 'table_document' not in saved
    assert saved['message'] == saved['copy_json']
    assert isinstance(saved['message'], str) and saved['message']
    assert 'fallback-private' not in saved['message']
    if 'fallback-private' not in invalid:
        assert saved['message'] == invalid
    assert value == before


_NONFINITE_CONTEXT_CASES = [
    ('{"number":NUMBER,"password":{"child":"context-private"},"safe":[0,false,null,"NaN"]}',
     '{"number":NUMBER,"password":"***","safe":[0,false,null,"NaN"]}'),
    ('{"password":["context-private"],"bad":NUMBER,"safe":"1e999"}',
     '{"password":"***","bad":NUMBER,"safe":"1e999"}'),
    ('{"path":"params.password","value":"context-private","bad":NUMBER,"safe":"Infinity"}',
     '{"path":"params.password","value":"***","bad":NUMBER,"safe":"Infinity"}'),
]


@pytest.mark.parametrize('template,expected_template', _NONFINITE_CONTEXT_CASES, ids=['object', 'array', 'path-value'])
def test_nonfinite_copy_keeps_sensitive_structure_and_numeric_spelling(template, expected_template):
    for number in ('0', 'NaN', 'Infinity', '-Infinity', '1e999'):
        body, expected = template.replace('NUMBER', number), expected_template.replace('NUMBER', number)
        assert clean(body) == expected
        value = fields({'value': 0}, Table(columns=['value'], rows=[[0]]))
        value['copy_json'] = body
        before = copy.deepcopy(value)
        saved = persisted(value)
        if number == '0':
            assert json.loads(saved['copy_json']) == json.loads(expected)
            assert saved['message'] == 'value\n0' and 'table_document' in saved
        else:
            assert 'table_document' not in saved
            assert saved['message'] == saved['copy_json'] == expected
        assert 'context-private' not in json.dumps(saved)
        assert value == before


@pytest.mark.parametrize('template,expected_template', _NONFINITE_CONTEXT_CASES, ids=['object', 'array', 'path-value'])
def test_valid_sdk_string_redacts_nonfinite_text_without_losing_other_content(template, expected_template):
    for number in ('0', 'NaN', 'Infinity', '-Infinity', '1e999'):
        body, expected = template.replace('NUMBER', number), expected_template.replace('NUMBER', number)
        nested = json.dumps({'raw': body})
        value = fields({'body': body, 'nested': nested, 'echo': 'context-private'},
                       Table(title='context-private', columns=['text'], rows=[[body]]))
        before = copy.deepcopy(value)
        saved = persisted(value)
        copied = json.loads(saved['copy_json'])
        assert copied['body'] == expected and copied['echo'] == '***'
        assert json.loads(copied['nested']) == {'raw': expected}
        assert saved['table_document']['tables'] == [{'title': '***', 'columns': ['text'], 'rows': [[expected]]}]
        assert saved['message'] == '***\ntext\n' + expected
        assert value == before


def test_json_fragment_detection_keeps_quoted_brackets_and_nested_fragment_semantics():
    text = 'prefix ' + json.dumps({'text': '[}', 'body': json.dumps({'password': 'HIDDEN'}),
                                  'values': [0, False, {'client_secret': 'HIDDEN'}]}) + ' suffix'
    saved = clean(text)
    assert json.loads(saved[len('prefix '):-len(' suffix')]) == {
        'text': '[}', 'body': '{"password":"***"}', 'values': [0, False, {'client_secret': '***'}]}
    assert clean("{'raw': '{\"password\":\"HIDDEN\"}'}") == "{'raw': '{\"password\":\"***\"}'}"


def empty_snapshot():
    return {'state': {'qpos': list(range(14)), 'eef': list(range(14))}, 'frames': {}, 'state_error': None}


def wait(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'recording condition timed out'
        time.sleep(.01)


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def large_record():
    raw = {'password': 'record-private', 'values': [
        {'x': number} if number % 2 == 0 else [number, False, None, ''] for number in range(1200)]}
    value = fields(raw, Table(title='record-private', columns=['value'], rows=[[row] for row in raw['values']]),
                   Table(columns=['field', 'value'], rows=[['auth.authorization', 'Bearer column-private'],
                                                           ['keep', 'known-private']]))
    return {**value, 'event': 'policy.log', 'instance_id': 'example-0'}, raw


def assert_large_record(saved, raw, *, known):
    assert json.loads(saved['copy_json']) == {**raw, 'password': '***'}
    assert saved['table_document']['tables'][0] == {
        'title': '***', 'columns': ['value'], 'rows': [[row] for row in raw['values']]}
    second = [['auth.authorization', '***'], ['keep', '***' if known else 'known-private']]
    assert saved['table_document']['tables'][1] == {'title': '', 'columns': ['field', 'value'], 'rows': second}
    assert saved['message'] == ('***\nvalue\n' + '\n'.join(json.dumps(row, separators=(',', ':')) for row in raw['values'])
                                + '\n\nfield\tvalue\nauth.authorization\t***\nkeep\t' + second[1][1])
    assert saved['instance_id'] == 'example-0'


def test_real_idle_writer_persists_complete_table_before_ack_drain(tmp_path):
    value, raw = large_record()
    service = RecordingService(tmp_path, 'table-idle', empty_snapshot)
    service.start()
    try:
        assert service.log(value)
        wait(lambda: service.status()['pending_bytes'] == 0)
        assert service.status()['state'] == 'idle'
    finally:
        assert not service.shutdown(time.monotonic() + 10)
    saved = read_rows(tmp_path / 'rc_table-idle.jsonl')
    assert len(saved) == 1, 'an ACK must not stand in for the missing table body'
    assert_large_record(saved[0], raw, known=False)


def test_real_active_writer_persists_all_copies_and_keeps_sampling(tmp_path):
    value, raw = large_record()
    errors = []
    service = RecordingService(tmp_path, 'table-active', empty_snapshot, on_error=errors.append)
    service.start()
    try:
        started = service.command('on', {'password': 'known-private'}).result(5)
        samples = service.status()['samples_written']
        assert service.log(value)
        assert service.policy_event(value)
        path = Path(started['path'])
        wait(lambda: (path / 'policy.jsonl').stat().st_size > 0)
        wait(lambda: service.status()['samples_written'] >= samples + 3)
        assert service.status()['state'] == 'recording'
        assert service.status()['error'] is None
        assert service.event('events', {'message': 'after table marker'})
        stopped = service.command('off').result(5)
        assert stopped['state'] == 'idle' and stopped['error'] is None
    finally:
        assert not service.shutdown(time.monotonic() + 10)
    assert not errors
    events = read_rows(path / 'events.jsonl')
    assert any(row.get('message') == 'after table marker' for row in events)
    for name in (tmp_path / 'rc_table-active.jsonl', path / 'events.jsonl', path / 'policy.jsonl'):
        tables = [row for row in read_rows(name) if row.get('event') == 'policy.log']
        assert len(tables) == 1
        assert_large_record(tables[0], raw, known=True)
    session = read_rows(path / 'session.jsonl')
    assert session[-1]['event'] == 'record.stopped'
    text = '\n'.join(item.read_text() for item in tmp_path.rglob('*.json*'))
    assert all(secret not in text for secret in ('record-private', 'column-private', 'known-private'))


@pytest.mark.parametrize('active', [False, True], ids=['idle', 'active'])
def test_real_writer_preserves_literal_and_invalid_copy_boundaries(tmp_path, active):
    secret = "a'\"b\\c"
    text = '[' * 1100 + '{"path":"params.password","value":"suffix-private"}'
    expected_text = '[' * 1100 + '{"path":"params.password","value":"***"}'
    records = [fields({'body': 'password=' + repr(secret)}, Table(columns=['echo'], rows=[[secret]])),
               fields(text, Table(columns=['text'], rows=[[text]]))]
    for invalid in ('NaN', 'Infinity', '1e999', '{"nested":[NaN, {"password":"fallback-private"}]}'):
        record = fields({'value': 0}, Table(columns=['value'], rows=[[0]]))
        records.append({**record, 'copy_json': invalid})
    context_cases = [(body.replace('NUMBER', number), expected.replace('NUMBER', number))
                     for (body, expected), number in zip(_NONFINITE_CONTEXT_CASES, ('NaN', '1e999', 'NaN'))]
    for body, _ in context_cases:
        record = fields({'value': 0}, Table(columns=['value'], rows=[[0]]))
        records.append({**record, 'copy_json': body})
    for body, _ in context_cases:
        records.append(fields({'body': body}, Table(columns=['text'], rows=[[body]])))
    records = [{**record, 'event': 'policy.log', 'case': index} for index, record in enumerate(records)]
    errors = []
    service = RecordingService(tmp_path, 'boundaries', empty_snapshot, on_error=errors.append)
    service.start()
    try:
        if active:
            path = Path(service.command('on').result(5)['path'])
            samples = service.status()['samples_written']
        for record in records:
            assert service.log(record)
            if active:
                assert service.policy_event(record)
        if active:
            wait(lambda: len(read_rows(path / 'policy.jsonl')) >= len(records))
            wait(lambda: service.status()['samples_written'] >= samples + 3)
            assert service.status()['state'] == 'recording' and service.status()['error'] is None
            assert service.event('events', {'message': 'after boundary records'})
            stopped = service.command('off').result(5)
            assert stopped['state'] == 'idle' and stopped['error'] is None
    finally:
        assert not service.shutdown(time.monotonic() + 10)
    assert not errors
    files = [tmp_path / 'rc_boundaries.jsonl']
    if active:
        files.extend([path / 'events.jsonl', path / 'policy.jsonl'])
        assert any(row.get('message') == 'after boundary records' for row in read_rows(path / 'events.jsonl'))
        assert read_rows(path / 'session.jsonl')[-1]['event'] == 'record.stopped'
    for file in files:
        saved = [row for row in read_rows(file) if row.get('event') == 'policy.log']
        assert len(saved) == len(records), 'ACK drain must include every actual persisted body'
        assert saved[0]['table_document']['tables'][0]['rows'] == [['***']]
        assert json.loads(saved[0]['copy_json']) == {'body': 'password=***'}
        assert saved[1]['message'] == 'text\n' + expected_text
        assert saved[1]['table_document']['tables'][0]['rows'] == [[expected_text]]
        assert json.loads(saved[1]['copy_json']) == expected_text
        for row in saved[2:9]:
            assert 'table_document' not in row and row['message'] == row['copy_json']
        for row, (_, expected) in zip(saved[6:9], context_cases):
            assert row['message'] == row['copy_json'] == expected
        for row, (_, expected) in zip(saved[9:], context_cases):
            assert json.loads(row['copy_json']) == {'body': expected}
            assert row['message'] == 'text\n' + expected
            assert row['table_document']['tables'][0]['rows'] == [[expected]]
        assert 'suffix-private' not in file.read_text() and 'fallback-private' not in file.read_text()
        assert 'context-private' not in file.read_text()
