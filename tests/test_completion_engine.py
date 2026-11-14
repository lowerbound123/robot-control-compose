"""Completion is tolerant analysis, never command execution or parser mutation."""
import pytest

from robot_control.completion import analyze, apply_completion


@pytest.fixture
def catalog():
    return {'sources': ['example', 'hold'], 'tools': ['state', 'send_command'],
            'pose_root': '/work/outputs/state_saves',
            'rc_config': {'robot': {'enabled': True}, 'frequency_hz': 50, 'auth': {'api_key': 'private'}},
            'instances': {'example-1': {'commands': {
                'choose': {'help': 'choose behavior', 'parameters': [
                    {'name': 'mode', 'type': 'enum', 'choices': ['fast', 'gentle move'], 'help': 'motion mode'},
                    {'name': 'count', 'type': 'int', 'help': 'repeat count'}]},
                'model': {'parameters': [{'name': 'file', 'type': 'path', 'root': '.', 'kind': 'file', 'suffixes': ['.json']}]},
            }, 'config_hints': {'settings': {
                'params.task': {'name': 'task', 'type': 'str', 'help': 'new task'},
                'execution.mode': {'type': 'enum', 'choices': ['sync', 'async']},
                'model': {'type': 'path', 'root': 'models'},
            }}}}}


@pytest.mark.parametrize('line,labels', [
    ('policy ', ['example-1', 'help', 'list', 'manage', 'switch']),
    ('policy example-1 ', ['choose', 'config', 'model', 'retry']),
    ('policy example-1 config ', ['save', 'set', 'show']),
    ('policy switch h', ['hold']),
    ('mcp s', ['send_command', 'state']),
    ('monitor ex', ['example-1']),
    ('camera ', ['hide', 'show', 'toggle']),
    ('policy example-1 config set exec', ['execution.mode']),
    ('policy example-1 config set execution.mode a', ['async']),
    ('config rc set robot.enabled ', ['false', 'true']),
])
def test_candidates_are_current_token_not_full_command(catalog, line, labels):
    result = analyze(line, len(line), catalog)
    assert sorted(item.label for item in result.items) == labels
    assert result.text == line and result.cursor == len(line)


def test_middle_token_replacement_preserves_other_arguments_and_unicode(catalog):
    line = 'policy example-1 choose fa 12 尾部'
    cursor = line.index('fa') + 1
    result = analyze(line, cursor, catalog)
    chosen = next(item for item in result.items if item.label == 'fast')
    updated, position = apply_completion(line, cursor, result, chosen)
    assert updated == 'policy example-1 choose fast 12 尾部'
    assert updated[position:] == '12 尾部'


@pytest.mark.parametrize('quote', ['"', "'"])
def test_unfinished_quote_is_tolerated_and_existing_quote_style_preserved(catalog, quote):
    line = 'policy example-1 choose ' + quote + 'gent'
    result = analyze(line, len(line), catalog)
    updated, cursor = apply_completion(line, len(line), result, result.items[0])
    assert updated == 'policy example-1 choose ' + quote + 'gentle move' + quote + ' '
    assert cursor == len(updated)


@pytest.mark.parametrize('quote', ['', "'", '"'])
def test_path_completion_roundtrips_special_filenames_through_execution_parser(catalog, quote):
    import shlex
    from robot_control.completion import CompletionItem
    for value in ('model$.json', 'model`.json', 'model\\$.json', '中文 文件.json',
                  'it\'s.json', 'a"b.json', 'a\\b.json', 'x\'"\\$` y.json'):
        line = 'policy example-1 model ' + quote + 'mo'
        result = analyze(line, len(line), catalog)
        updated, cursor = apply_completion(line, len(line), result,
                                           CompletionItem(value, value, kind='file'))
        assert shlex.split(updated) == ['policy', 'example-1', 'model', value]
        # Re-opening Tab at the completed token must query the same filename.
        reopened = analyze(updated, cursor - 1, catalog)
        assert reopened.path_request['prefix'] == value


def test_raw_config_tail_keeps_spaces_and_json_without_shell_reinterpretation(catalog):
    line = 'policy example-1 config set params.task "中文 \\"quote\\" text"'
    result = analyze(line, len(line), catalog)
    assert line[result.start:result.end] == '"中文 \\"quote\\" text"'
    assert 'task' in result.hint and 'new task' in result.hint
    assert result.lint == '' and result.items == []


def test_unknown_setting_warns_without_rejecting_raw_value(catalog):
    line = 'policy example-1 config set opaque_key { unfinished value'
    result = analyze(line, len(line), catalog)
    assert '未在提示声明中' in result.lint
    assert line[result.start:result.end] == '{ unfinished value'


def test_enum_suggestions_keep_declared_order(catalog):
    line = 'policy example-1 config set execution.mode '
    assert [item.label for item in analyze(line, len(line), catalog).items] == ['sync', 'async']
    catalog['instances']['example-1']['commands']['choose']['parameters'][0]['choices'] = ['z-last', 'a-first']
    line = 'policy example-1 choose '
    assert [item.label for item in analyze(line, len(line), catalog).items] == ['z-last', 'a-first']


def test_argument_hint_and_lint_follow_current_parameter(catalog):
    line = 'policy example-1 choose fast nope'
    result = analyze(line, len(line), catalog)
    assert 'count' in result.hint and 'int' in result.hint and 'repeat count' in result.hint
    assert result.lint
    line = 'policy example-1 choose fast 3'
    assert not analyze(line, len(line), catalog).lint


def test_path_request_contains_only_declared_selector_and_prefix(catalog):
    line = 'policy example-1 model mo'
    result = analyze(line, len(line), catalog)
    assert result.path_request['instance'] == 'example-1'
    assert result.path_request['selector'] == {'command': 'model', 'argument': 0}
    assert result.path_request['prefix'] == 'mo'
    line = 'policy example-1 config set model nested/'
    result = analyze(line, len(line), catalog)
    assert result.path_request['selector'] == {'setting': 'model'}
    assert result.path_request['prefix'] == 'nested/'


def test_directory_selection_has_no_delimiter_and_stale_result_cannot_edit(catalog):
    from robot_control.completion import CompletionItem
    line = 'policy example-1 model "nested'
    result = analyze(line, len(line), catalog)
    changed, cursor = apply_completion(line, len(line), result,
                                       CompletionItem('nested folder/', 'nested folder/', kind='directory'))
    assert changed == 'policy example-1 model "nested folder/'
    assert cursor == len(changed)
    with pytest.raises(ValueError):
        apply_completion(line + 'x', len(line) + 1, result, CompletionItem('x', 'x'))


def test_rc_keys_exclude_secrets_and_pose_is_local_path_request(catalog):
    result = analyze('config rc set ', 14, catalog)
    assert 'auth.api_key' not in [item.label for item in result.items]
    line = 'pose restore de'
    result = analyze(line, len(line), catalog)
    assert result.path_request['instance'] is None
    assert result.path_request['base_dir'] == '/work/outputs/state_saves'


def test_legacy_command_has_no_candidates(catalog):
    result = analyze('example-1-choose ', 17, catalog)
    assert not result.items and result.lint


def test_escaped_trailing_quote_is_not_mistaken_for_a_closed_quote(catalog):
    from robot_control.completion import CompletionItem
    line = 'policy example-1 model "has\\"'
    result = analyze(line, len(line), catalog)
    updated, cursor = apply_completion(line, len(line), result,
                                       CompletionItem('folder/', 'folder/', kind='directory'))
    assert updated == 'policy example-1 model "folder/'
    assert cursor == len(updated)


def test_config_enum_json_looking_string_keeps_string_type(catalog):
    catalog['instances']['example-1']['config_hints']['settings']['literal'] = {
        'type': 'enum', 'choices': ['null']}
    line = 'policy example-1 config set literal n'
    result = analyze(line, len(line), catalog)
    updated, _ = apply_completion(line, len(line), result, result.items[0])
    assert updated == 'policy example-1 config set literal "null"'


def test_raw_config_completion_does_not_append_space_to_policy_owned_value(catalog):
    from rc_policy_sdk.command_syntax import parse_command
    from robot_control.completion import CompletionItem
    line = 'policy example-1 config set execution.mode a'
    result = analyze(line, len(line), catalog)
    updated, cursor = apply_completion(line, len(line), result, result.items[0])
    assert parse_command(updated).value == 'async'
    assert cursor == len(updated)
    line = 'policy example-1 config set model mo'
    result = analyze(line, len(line), catalog)
    updated, cursor = apply_completion(line, len(line), result,
                                       CompletionItem('model.json', 'models/model.json', kind='file'))
    assert parse_command(updated).value == 'models/model.json'
    assert cursor == len(updated)


def test_unquoted_directory_with_spaces_leaves_cursor_inside_generated_quote(catalog):
    from robot_control.completion import CompletionItem
    line = 'policy example-1 model fol'
    result = analyze(line, len(line), catalog)
    updated, cursor = apply_completion(line, len(line), result,
                                       CompletionItem('folder name/', 'folder name/', kind='directory'))
    assert updated[:cursor].endswith('folder name/')
    assert updated[cursor:] == "'"


def test_primitive_metadata_choices_remain_typed_suggestions(catalog):
    catalog['instances']['example-1']['commands']['choose']['parameters'][1]['choices'] = [1, 3]
    line = 'policy example-1 choose fast '
    result = analyze(line, len(line), catalog)
    assert [item.insert_text for item in result.items] == ['1', '3']
    catalog['instances']['example-1']['config_hints']['settings']['enabled'] = {
        'type': 'bool', 'choices': [True]}
    line = 'policy example-1 config set enabled '
    result = analyze(line, len(line), catalog)
    assert [item.insert_text for item in result.items] == ['true']


def test_record_auto_mode_completions():
    from robot_control.completion import analyze
    result=analyze('record auto ',len('record auto '),{})
    assert {item.insert_text for item in result.items}=={'on','off'}
