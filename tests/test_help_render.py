"""Help layout keeps source text separate from its terminal decoration."""

import pytest

from robot_control.help_document import document, help_text
from robot_control.structured_log import clean_text, format_record


def sample_help():
    return document(
        'Policy 参数帮助',
        '中文说明与字面量 [bold] 标签。',
        usage=['policy example-0 configure <key> <value>'],
        commands=[
            {'group': '观察', 'command': 'policy example-0 status', 'summary': '读取当前状态'},
            {'group': '配置', 'command': 'policy example-0 config', 'summary': '查看配置'},
        ],
        sections=[{'title': '执行影响', 'body': '仅更新当前实例；保留已提交动作。'}],
        parameters=[
            {'name': 'gain', 'type': 'number', 'required': False, 'default': 1,
             'description': '速度比例，修改后在下一次动作生效。'},
            {'name': '目标阈值', 'type': 'number', 'required': True, 'choices': [0.1, 0.5, 1],
             'description': '这是需要完整保留的中文参数说明，包含空格 alpha beta 和 [bold] 字面量，'
                            '即使终端变窄也不能截断或把软换行写进复制内容。'},
        ],
        examples=[{'title': 'JSON 示例', 'command': 'policy example-0 configure gain {"value": 1}'}],
    )


def formatted_help(value, width):
    return format_record(help_text(value), width=width, help_document=value)


def test_help_with_final_json_example_is_expanded_and_copies_entire_help():
    value = document('命令帮助', examples=[{'title': '', 'command': '{"x": 1}'}])
    result = formatted_help(value, 120)
    assert result.parsed is None
    assert result.copy_text == '命令帮助\n\n示例\n{"x": 1}'
    assert result.document == result.copy_text
    assert len(result.rows) == 4


def test_wide_parameters_form_columns_and_narrow_parameters_stack_without_losing_source():
    value = sample_help()
    wide, narrow = (formatted_help(value, width) for width in (120, 50))
    wide_name = next(row for row in wide.rows if 'gain' in row.text.plain and 'number' in row.text.plain)
    assert '│' in wide_name.text.plain
    narrow_name = next(row for row in narrow.rows if row.text.plain == 'gain')
    assert narrow_name.text.plain == 'gain'
    assert not any('│' in row.text.plain for row in narrow.rows)
    assert wide.document == narrow.document == help_text(value)
    assert wide.copy_text == narrow.copy_text == help_text(value)
    assert '[bold]' in ''.join(row.text.plain for row in narrow.rows)
    for result, width in ((wide, 120), (narrow, 50)):
        assert all(row.text.cell_len <= width - 2 for row in result.rows)
        # Every mapped glyph comes from the canonical source, never padding or borders.
        for row in result.rows:
            if row.spans is not None:
                for span in row.spans:
                    assert row.text.plain[span.display_start:span.display_start + span.end - span.start] == result.document[span.start:span.end]


@pytest.mark.parametrize('width', [4, 12, 50, 120])
def test_long_parameter_names_and_control_bytes_remain_readable_and_literal(width):
    value = document('帮助', '字面量 [bold]\x1b[31m\r\n文本\t结束', parameters=[{
        'name': 'config.runtime.' + 'very_long_parameter_' * 5,
        'type': 'object', 'required': False, 'description': '中文描述' * 16,
        'default': {'x': '[bold]'},
    }])
    result = formatted_help(value, width)
    assert result.document == clean_text(help_text(value))
    assert result.copy_text == help_text(value)
    assert '\x1b' not in ''.join(row.text.plain for row in result.rows)
    assert all(row.text.cell_len <= width - 2 for row in result.rows)
    for row in result.rows:
        if row.spans is None:
            assert row.text.plain == result.document[row.start:row.end]
        else:
            for span in row.spans:
                assert row.text.plain[span.display_start:span.display_start + span.end - span.start] == result.document[span.start:span.end]


def test_mismatched_help_metadata_cannot_replace_logged_message_or_parse_its_json():
    result = format_record('original {"x": 1}', width=90, help_document=document('different'))
    assert result.document == 'original {"x": 1}'
    assert result.copy_text == 'original {"x": 1}'
    assert result.parsed is None
