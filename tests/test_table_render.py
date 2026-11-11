"""Explicit table metadata is lossless, literal and ordered at every width."""
import json

import pytest
from rich.text import Text

from rc_policy_sdk.tables import table_document_text
from robot_control.structured_log import DisplayRow, DisplaySpan, FormattedRecord, clean_text, format_record


def event_tables():
    return {'schema_version': 1, 'tables': [
        {'title': '运行 [bold]', 'columns': ['name', 'value'], 'rows': [
            ['zero', 0], ['flag', False], ['none', None], ['empty', ''],
            ['路径', '/完整/路径/' + 'abcdef中文' * 16],
            ['nested', {'z': [False, 0, None, ''], 'a': {}}],
            ['literal', '前\t后\r\n[red]\x1b[31m\x00结束']]},
        {'title': '空结果', 'columns': ['名称', '说明'], 'rows': []}]}


def assert_source(result):
    previous = 0
    for row in result.rows:
        spans = row.spans if row.spans is not None else (DisplaySpan(0, row.start, row.end),)
        for span in spans:
            assert span.start >= previous
            assert span.end >= span.start
            assert row.text.plain[span.display_start:span.display_start + span.end - span.start] == result.document[span.start:span.end]
            previous = span.end


@pytest.mark.parametrize('width', [4, 12, 50, 120])
def test_explicit_tables_keep_full_projection_and_original_json(width):
    value = event_tables()
    message = table_document_text(value)
    original = '{\n  "flag" : false, "raw": [0, null, ""]\n}'
    result = format_record(message, table_document=value, copy_json=original, width=width)
    assert result.parsed is None and result.source_complete
    assert result.document == clean_text(message)
    assert result.copy_text == original
    assert '无记录' in result.document and '\x1b' not in result.document
    assert_source(result)
    assert all(row.text.cell_len <= max(2, width - 2) for row in result.rows if not row.header)
    shown = ''.join(row.text.plain for row in result.rows)
    assert '""' in shown
    mapped = ''.join(row.text.plain[span.display_start:span.display_start + span.end - span.start]
                     for row in result.rows for span in row.spans or ())
    for item in ('false', 'null', '0'):
        assert item in mapped


def test_only_final_column_wraps_and_prefix_overflow_stacks_whole_table():
    value = {'schema_version': 1, 'tables': [{'title': '', 'columns': ['A', 'B'],
        'rows': [['first', 'xyz ' * 45], ['second', 'tail']]}]}
    message = table_document_text(value)
    wide = format_record(message, table_document=value, copy_json='{}', width=60)
    initial = next(row for row in wide.rows if 'first' in row.text.plain)
    assert len(initial.spans) == 2
    index = wide.rows.index(initial)
    assert len(wide.rows[index + 1].spans) == 1
    assert 'first' not in wide.rows[index + 1].text.plain
    narrow = format_record(message, table_document=value, copy_json='{}', width=8)
    assert all(len(row.spans or ()) <= 1 for row in narrow.rows)
    for result in (wide, narrow):
        assert_source(result)
        assert result.document == clean_text(message)


@pytest.mark.parametrize('prefix', ['first\nsecond', 'first\r\nsecond', 'a' * 200])
def test_multiline_or_oversize_prefix_stacks_all_rows(prefix):
    value = {'schema_version': 1, 'tables': [{'title': 'stack', 'columns': ['A', 'B'],
        'rows': [[prefix, 'one'], ['tiny', 'two']]}]}
    result = format_record(table_document_text(value), table_document=value, copy_json='{}', width=80)
    assert all(len(row.spans or ()) <= 1 for row in result.rows)
    assert_source(result)


def test_segment_mapping_handles_tab_columns_and_crlf_across_boundaries():
    value = {'schema_version': 1, 'tables': [{'title': '标题\r', 'columns': ['a\t头', 'b\r'],
        'rows': [['x\t', 'y\r'], ['', '\n尾']]}]}
    message = table_document_text(value)
    result = format_record(message, label='[RC] ', table_document=value, copy_json='{}', width=90)
    assert result.document == '[RC] ' + clean_text(message)
    assert_source(result)


@pytest.mark.parametrize('metadata', [None, {'schema_version': 99, 'tables': []},
    {'schema_version': 1, 'tables': [{'title': '', 'columns': ['a'], 'rows': [[1, 2]]}]},
    {'schema_version': 1, 'tables': [{'title': '', 'columns': ['a'], 'rows': [[2]]}]}])
def test_bad_or_mismatched_metadata_uses_original_plain_message(metadata):
    result = format_record('original [bold] message', table_document=metadata, copy_json='{ "keep": 1 }')
    assert result.document == 'original [bold] message'
    assert result.copy_text == 'original [bold] message'
    assert not result.source_complete


def test_ordinary_json_table_keys_are_still_json():
    raw = json.dumps({'table_document': event_tables(), 'copy_json': '{}'})
    result = format_record(raw, width=60)
    assert result.parsed is not None and len(result.rows) == 1
    assert result.copy_text == raw


def test_source_locator_prefers_real_spans_over_duplicate_decorative_offsets():
    result = FormattedRecord('ABC DEF GHI', (
        DisplayRow(0, 7, Text('ABC │ DEF'), spans=(DisplaySpan(0, 0, 3), DisplaySpan(6, 4, 7))),
        DisplayRow(8, 8, Text('────'), spans=()),
        DisplayRow(8, 11, Text('GHI'), spans=(DisplaySpan(0, 8, 11),)),
        DisplayRow(8, 8, Text('────'), spans=()),
    ), '{}', None, True)
    assert result.row_for_offset(9) == 2
    assert result.row_for_offset(8) == 2
    assert result.row_for_offset(5) == 0
    assert result.row_for_offset(3) == 0


def test_crlf_crossing_title_and_row_separator_does_not_add_phantom_blank_rows():
    value = {'schema_version': 1, 'tables': [{'title': 'title\r', 'columns': ['A', 'B\r'],
        'rows': [['x', 'first\r'], ['y', 'last']]}]}
    result = format_record(table_document_text(value), table_document=value, copy_json='{}', width=80)
    assert_source(result)
    assert not any('""' in row.text.plain for row in result.rows[1:])
    assert not any(not row.text.plain.strip() for row in result.rows[1:])


@pytest.mark.parametrize('copy_json', [None, 7, '', '{broken', 'NaN', '1e999'])
def test_invalid_original_copy_metadata_falls_back_without_changing_plain_text(copy_json):
    value = {'schema_version': 1, 'tables': [{'title': '', 'columns': ['A'], 'rows': [[0]]}]}
    message = table_document_text(value)
    result = format_record(message, table_document=value, copy_json=copy_json)
    assert result.document == clean_text(message)
    assert result.copy_text == message and not result.source_complete
