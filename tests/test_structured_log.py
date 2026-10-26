import json

import pytest

from robot_control.structured_log import format_record, parse_json_message


@pytest.mark.parametrize("message,prefix,raw,suffix", [
    (' {"中文": [1, true, null]} ', ' ', '{"中文": [1, true, null]}', ' '),
    ('MCP state: {\n "x": 1\n}', 'MCP state: ', '{\n "x": 1\n}', ''),
    ('policy command [A:1] succeeded: [1,2]', 'policy command [A:1] succeeded: ', '[1,2]', ''),
    ('policy command [123] succeeded: {"x":1}', 'policy command [123] succeeded: ', '{"x":1}', ''),
    ('[llm_direction] {"decision":"noop"}', '[llm_direction] ', '{"decision":"noop"}', ''),
    ('MCP images: {}\n图像内容未显示（3 张）；请查看 images 元数据。', 'MCP images: ', '{}', '\n图像内容未显示（3 张）；请查看 images 元数据。'),
])
def test_json_detection_preserves_original_payload_and_surrounding_text(message, prefix, raw, suffix):
    parsed = parse_json_message(message)
    assert (parsed.prefix, parsed.raw, parsed.suffix) == (prefix, raw, suffix)
    assert json.loads(parsed.pretty) == json.loads(raw)


@pytest.mark.parametrize("message", [
    '{"incomplete":', '{"bad": {"ok": 1}', '[broken {"x": 1}]',
    '42', 'null', '"hello"', '[bold] ordinary text', 'first {} second {}',
    '{"x": NaN}', 'part one {\n', '"a": 1\n}',
    'MCP state: {"bad": {"ok":1}',
    'MCP state: [0, {"ok":1}',
    'policy command [A:1] succeeded: {"bad": {"ok":1}',
    '[hold] {"bad": {"ok":1}', '[hold] [0, {"ok":1}',
])
def test_invalid_scalars_and_separate_events_stay_plain(message):
    assert parse_json_message(message) is None


def test_collapsed_json_is_one_row_expansion_is_colored_and_original_copy_is_exact():
    raw = '{\n  "key": "长文本' + '中' * 30 + '", "n": 2.5, "b": false, "x": null\n}'
    folded = format_record(raw, label='[INFO] ', width=36)
    assert len(folded.rows) == 1
    assert folded.rows[0].text.plain.endswith('…')
    assert folded.copy_text == raw
    expanded = format_record(raw, label='[INFO] ', width=36, expanded=True, parsed=folded.parsed)
    assert expanded.document.startswith('[INFO] {')
    assert '\n  "key":' in expanded.document
    assert len(expanded.rows) > 6
    styles = {str(span.style) for row in expanded.rows for span in row.text.spans}
    assert len(styles) >= 5
    assert all(row.text.cell_len <= 34 for row in expanded.rows)


def test_plain_wrapping_preserves_every_character_and_control_codes_are_literal():
    raw = 'prefix [bold]\x1b[31m\n' + '中文abcdef' * 30
    formatted = format_record(raw, width=24)
    assert formatted.parsed is None
    assert '\x1b' not in formatted.document and '[bold]' in formatted.document
    assert formatted.copy_text == raw
    assert len(formatted.rows) > 10
    # Offsets refer to the original logical text, not the wrapped screen rows.
    for row in formatted.rows:
        assert row.text.plain == formatted.document[row.start:row.end]


def test_json_markup_and_escape_sequences_are_never_interpreted():
    raw = '{"s":"[bold]\\n\\u001b[31m"}'
    result = format_record(raw, width=80, expanded=True)
    assert '[bold]' in result.document
    assert '\x1b' not in result.document
    assert result.copy_text == raw
