"""Main configuration YAML is data; roundtrip editing preserves its presentation."""
from pathlib import Path

import pytest


def codec():
    from robot_control import yaml_config
    return yaml_config


def test_plain_types_unicode_and_aliases_are_independent(tmp_path):
    path = tmp_path / 'rc.yaml'
    path.write_text('''title: "中文: # 文本"
text: |
  第一行
  第二行
flag: &flag true
same_flag: *flag
number: 1
fraction: 1.0
nothing: null
base: &base {items: [1, false]}
other: *base
word: on
''')
    value = codec().load_yaml_config(path)
    assert type(value['flag']) is type(value['same_flag']) is bool
    assert type(value['number']) is int and type(value['fraction']) is float
    assert value['title'] == '中文: # 文本' and value['text'] == '第一行\n第二行\n'
    assert value['nothing'] is None and value['word'] == 'on'
    assert type(value['base']) is dict and type(value['base']['items']) is list
    value['base']['items'].append(7)
    assert value['other']['items'] == [1, False]


@pytest.mark.parametrize('body', [
    'password: private-secret\nbroken: [',
    'password: private-secret\nx: 1\nx: 2\n',
    'password: private-secret\nx: .nan\n',
    'password: private-secret\nx: 2026-10-29\n',
    'password: private-secret\nx: !custom tag\n',
    'password: &same private-secret\nother: &same value\ncopy: *same\n',
    'password: private-secret\nx: !!python/object/apply:os.system [echo forbidden]\n',
    'password: private-secret\n1: bad\n',
    'password: private-secret\na: &a [*a]\n',
    'password: private-secret\na: &a {x: 1}\nb: {<<: *a}\n',
    '%YAML 1.1\n---\npassword: private-secret\nx: on\n',
    'password: private-secret\n---\nx: 2\n',
    '[private-secret]\n',
])
def test_rejects_invalid_or_non_json_yaml_without_leaking_values(tmp_path, body):
    path = tmp_path / 'policy.yaml'
    path.write_text(body)
    with pytest.raises(ValueError) as error:
        codec().load_yaml_config(path)
    assert 'private-secret' not in str(error.value)
    assert str(path) in str(error.value)
    assert path.read_text() == body


def test_save_retains_comments_styles_and_order_and_removes_stale_keys(tmp_path):
    path = tmp_path / 'policy.yaml'
    path.write_text('''# owner note
name: "中文" # name note
params: # params note
  gain: 1.0 # tuning note
  message: |
    line 1
    line 2
obsolete: yes
''')
    value = codec().load_yaml_config(path)
    value['params']['gain'] = 2.0
    value.pop('obsolete')
    value['new'] = False
    codec().save_yaml_config(path, value)
    output = path.read_text()
    assert all(note in output for note in ('# owner note', '# name note', '# params note', '# tuning note'))
    assert 'name: "中文"' in output and 'message: |' in output
    assert 'obsolete' not in output
    assert output.index('name:') < output.index('params:') < output.index('new:')
    assert codec().load_yaml_config(path) == value


@pytest.mark.parametrize('before,after,kind', [('true',1,int), ('1',True,bool), ('1',1.0,float), ('1.0',1,int)])
def test_saving_distinguishes_equal_values_with_different_types(tmp_path, before, after, kind):
    path = tmp_path / 'rc.yaml'
    path.write_text(f'value: {before} # typed\n')
    codec().save_yaml_config(path, {'value': after})
    assert type(codec().load_yaml_config(path)['value']) is kind
    assert '# typed' in path.read_text()


def test_editing_alias_does_not_change_other_fields(tmp_path):
    path = tmp_path / 'policy.yaml'
    path.write_text('base: &base\n  value: 1 # keep\ncopy: *base\n')
    value = codec().load_yaml_config(path)
    value['copy']['value'] = 2
    codec().save_yaml_config(path, value)
    result = codec().load_yaml_config(path)
    assert result == {'base': {'value': 1}, 'copy': {'value': 2}}
    assert '# keep' in path.read_text()


def test_list_comments_follow_stable_occurrences(tmp_path):
    path = tmp_path / 'policy.yaml'
    path.write_text('''policies: # chain
  - a # first a
  - b # b cell
  - a # second a
''')
    codec().save_yaml_config(path, {'policies': ['b','a','a','c']})
    output = path.read_text()
    assert '# chain' in output
    assert output.index('# b cell') < output.index('# first a') < output.index('# second a')
    assert codec().load_yaml_config(path)['policies'] == ['b','a','a','c']


def test_template_comments_added_and_noop_save_is_byte_identical(tmp_path):
    path, template = tmp_path/'rc.yaml', tmp_path/'rc.yaml.example'
    path.write_text('# local\na: 3 # custom\n')
    template.write_text('a: 1\nb: false # new default\n')
    output = codec().render_yaml_config(path, {'a':3,'b':False}, template=template)
    assert b'# custom' in output and b'# new default' in output
    path.write_bytes(output)
    assert codec().render_yaml_config(path, {'a':3,'b':False}) == output


def test_save_failure_keeps_original_and_removes_temporary(tmp_path, monkeypatch):
    path = tmp_path/'rc.yaml'
    path.write_text('a: 1\n')
    def fail(*args): raise OSError('injected replacement failure')
    monkeypatch.setattr(codec().os, 'replace', fail)
    with pytest.raises(OSError): codec().save_yaml_config(path, {'a':2})
    assert path.read_text() == 'a: 1\n'
    assert list(tmp_path.iterdir()) == [path]
