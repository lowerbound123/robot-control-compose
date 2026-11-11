"""Large legal string cells must not stall RC publication or reveal keys."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from robot_control.secret_config import public_message


_ENCODED_VALUE = json.dumps({'raw': json.dumps({'api_key': 'HIDDEN'})})


@pytest.mark.parametrize('text', [
    'payload ' + _ENCODED_VALUE + ': done',
    '{bad ' + _ENCODED_VALUE + ':0}',
    '{bad (' + _ENCODED_VALUE + '):0}',
    '{bad [' + _ENCODED_VALUE + ']:0}',
    "{'" + r'{"\u0061pi_key":"HIDDEN"}' + "': 0}",
    "{r'" + r'{"\u0061pi_key":"HIDDEN"}' + "': 0}",
])
def test_fix2_visible_json_survives_container_colons_and_actual_string_keys(text):
    clean = public_message(text)
    assert 'HIDDEN' not in clean
    assert '***' in clean


def test_json_encoded_string_key_retains_its_existing_nonredaction_semantics():
    text = json.dumps({json.dumps({'api_key':'HIDDEN'}):0})
    assert public_message(text) == text


@pytest.mark.parametrize('literal', ['123 + 456j', '123-456j', '-123 + 456j',
    '(123) + (456j)', '(-123 - 456j)', '1e2 + 0.5e3j'])
def test_fix2_api_key_masks_complete_python_literal(literal):
    import ast
    text = "{'api_key': " + literal + ", 'keep':0}"
    assert isinstance(ast.literal_eval(text)['api_key'], complex)
    assert ast.literal_eval(public_message(text)) == {'api_key':'***','keep':0}


def test_fix2_nested_closed_container_colons_have_bounded_cost():
    code = '''
from robot_control.secret_config import public_message
text = '{' * 8000 + '"safe"' + '}:0' * 8000
assert len(text) == 32006
for _ in range(5):
    assert public_message(text) == text
'''
    env = {**os.environ, 'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
    result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('text', [
    'ordinary key starts " then {"\\u0061pi_key":"HIDDEN"}',
    "can't {'\\u0061pi_key':'HIDDEN'}",
    "{'api_key': '''HIDDEN'''}",
    "{'api_key': 'prefix' 'HIDDEN'}",
    "{'api_' 'key': 'HIDDEN'}",
    "{'path': r'params.api_key', 'value': 'HIDDEN'}",
    "{'path': u'params.api_key', 'value': 'HIDDEN'}",
    "{'path': 'params.' 'api_key', 'value': 'HIDDEN'}",
    "{'value': '''prefix\nHIDDEN''', 'path': (r'params.' u'api_key')}",
    "{('api_' 'key'): b'HIDDEN'}",
    "{broken ordinary starts \" then {\"\\u0061pi_key\":\"HIDDEN\"}}",
    '{broken # {"\\u0061pi_key":"HIDDEN"}}',
    "{'api_' # ordinary comment\n 'key': 'HIDDEN'}",
    "{'api_key': 'prefix' " + chr(92) + "\n 'HIDDEN'}",
    "{('api_' " + chr(92) + "\n 'key'): 'HIDDEN'}",
    "{'path': r'params.' " + chr(92) + "\n u'api_key', 'value':'HIDDEN'}",
])
def test_review_r1_complete_python_literals_and_quoted_prefixes(text):
    clean = public_message(text)
    assert 'HIDDEN' not in clean
    assert '***' in clean


@pytest.mark.parametrize('text', [
    'before {broken {"api_key":"HIDDEN"}} after',
    'before [broken {"\\u0061pi_key":"HIDDEN"}] after',
    "'not a valid\n literal {\"\\u0061pi_key\":\"HIDDEN\"}\n'",
    "before {broken {'api_key': 'HIDDEN'}} after",
    '{"path":"params.api_key", "value":"HIDDEN"}',
    '{"value":"HIDDEN", "path":"params.api_key"}',
    "{'value':'HIDDEN', 'path':'params.api_key'}",
    json.dumps({'raw':json.dumps({'api_key':'HIDDEN'})}),
    "{'raw': '{\"api_key\":\"HIDDEN\"}'}",
    '{"' + ''.join('\\u%04x' % ord(c) for c in 'api_key') + '":"HIDDEN"}',
    '{"api_key":{"nested":"HIDDEN"},"kept":0}',
])
def test_existing_embedded_json_repr_and_path_redaction_survives_invalid_shells(text):
    assert 'HIDDEN' not in public_message(text)
    assert '***' in public_message(text)


@pytest.mark.parametrize('text', [
    '[bold]中文[/bold]\n{not a JSON object}\t0 false null',
    'before {"text":"brace { in a string", "values":[false,0,null]} after',
    "prefix {'safe': ('quoted', 0, False, None)} suffix",
    'before {bad {"ordinary":0}} after',
    '{' * 1000,
])
def test_nonsecret_text_remains_byte_for_byte_unchanged(text):
    assert public_message(text) == text


def test_large_invalid_brackets_are_bounded_without_truncation():
    code = r'''
import json
from robot_control.secret_config import public_message
for text in (
    '{' * 50000,
    '{' * 50000 + r'{"\u0061pi_key":"HIDDEN"}',
    '[' * 50000 + r'bad {"\u0061pi_key":"HIDDEN"}' + ']' * 50000,
    '[' * 50000 + 'bad {"value":"HIDDEN","path":"params.api_key"}' + ']' * 50000,
    '{ # {' * 10000 + r'{"\u0061pi_key":"HIDDEN"}',
):
    clean = public_message(text)
    assert 'HIDDEN' not in clean
    assert clean.count('[') == text.count('[')
    assert clean.count('}') == text.count('}')
    if 'HIDDEN' not in text: assert clean == text
'''
    env = {**os.environ, 'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')}
    result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_review_r2_many_secrets_and_large_legal_sdk_log_have_bounded_publication_cost():
    code = r'''
import json, threading
from types import SimpleNamespace
from rc_policy_sdk import Table, TableOutput
from rc_policy_sdk.runtime import PolicyRuntime
from rc_policy_sdk.outbox import PriorityOutbox, PrioritySender
from robot_control.table_output import public_table_fields
sender = PrioritySender.__new__(PrioritySender)
sender.outbox = PriorityOutbox(64)
sender.lock = threading.Lock()
sender.failure = None
runtime = PolicyRuntime(SimpleNamespace(context_id='c', config_version=1,
    table_output_v1=True, send_log=sender.send_log), {'context_id':'c','config_version':1})
assert runtime.log_table(TableOutput(result=[{'api_key':f'SECRET_{n:08d}'} for n in range(5000)],
    tables=[Table(columns=['large'], rows=[['x'*400000]])]))
frame = sender.outbox.get_nowait()[0]
assert len(frame) == 950196 and len(frame) < sender.outbox.log_maxbytes
event = json.loads(frame[4:])
for _ in range(10):
    fields = public_table_fields(event['result'], event['table_document'])
    assert fields['table_document']['tables'][0]['rows'] == [['x'*400000]]
    assert 'SECRET_' not in fields['copy_json']
overlap = TableOutput(result=[{'api_key':'a'*n} for n in range(1, 501)],
    tables=[Table(columns=['large'], rows=[['a'*200000+' END']])])
assert runtime.log_table(overlap)
overlap_frame = sender.outbox.get_nowait()[0]
assert len(overlap_frame) < sender.outbox.log_maxbytes
event = json.loads(overlap_frame[4:])
fields = public_table_fields(event['result'], event['table_document'])
assert fields['table_document']['tables'][0]['rows'] == [['*** END']]
'''
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, 'PYTHONPATH':os.pathsep.join(str(root / path) for path in ('src','packages/policy-sdk/src'))}
    result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_known_secret_matches_hide_overlaps_and_unicode_without_replacing_unrelated_text():
    from rc_policy_sdk import Table, TableOutput
    from rc_policy_sdk.tables import normalize_table_output
    from robot_control.table_output import public_table_fields
    output = TableOutput(result=[{'api_key':s} for s in ('aba','bab','北京','京城')],
        tables=[Table(title='untouched', columns=['text'], rows=[['ababa 北京城 trailing']])])
    fields = public_table_fields(*normalize_table_output(output))
    assert fields['table_document']['tables'][0] == {
        'title':'untouched','columns':['text'],'rows':[['*** *** trailing']]}
