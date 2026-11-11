"""Semantic help layout for the log formatter worker, with literal Rich text."""

from rich.cells import cell_len
from rich.text import Text

from .help_document import HelpLine, help_lines, parameter_text
from .structured_log import DisplayRow, DisplaySpan, FormattedRecord, _log_text, _rows, clean_text


_STYLES = {
    'title': 'bold bright_cyan',
    'summary': '',
    'section': 'bold bright_blue',
    'usage': 'cyan',
    'catalog': '',
    'parameter': '',
    'example': 'green',
    'body': '',
    'blank': '',
}


def _clean_block(text, *, followed_by_newline):
    # HelpLine is split before sanitizing. Retain the real following newline
    # while normalizing so a CRLF at the boundary is still only one newline.
    return clean_text(text + '\n')[:-1] if followed_by_newline else clean_text(text)


def _inset_rows(text, offset, width, prefix):
    """Indent source text without adding padding to its logical offsets."""
    for row in _rows(text, offset, max(1, width - cell_len(prefix))):
        decorated = Text()
        decorated.append(prefix, style='dim')
        decorated.append_text(row.text)
        yield DisplayRow(row.start, row.end, decorated,
                         spans=(DisplaySpan(len(prefix), row.start, row.end),))


def _parameter_rows(parameter, block, offset, width, name_width):
    name = clean_text(parameter['name'])
    lines = block.split('\n')
    if '\n' in name or not lines[0].startswith(name + '  '):
        for line in lines:
            yield from _rows(Text(line), offset, width)
            offset += len(line) + 1
        return
    metadata_offset = offset + len(name) + 2
    flags = Text(lines[0][len(name) + 2:], style='cyan')
    if width >= 72 and cell_len(name) <= name_width:
        for index, row in enumerate(_rows(flags, metadata_offset, width - name_width - 3)):
            shown_name = name if index == 0 else ''
            text = Text()
            text.append(shown_name, style='bold magenta')
            spans = [DisplaySpan(0, offset, offset + len(name))] if index == 0 else []
            text.append(' ' * (name_width - cell_len(shown_name)) + ' │ ', style='dim')
            spans.append(DisplaySpan(len(text), row.start, row.end))
            text.append_text(row.text)
            yield DisplayRow(offset if index == 0 else row.start, row.end, text, spans=tuple(spans))
        prefix = ' ' * name_width + ' │ '
    else:
        styled_name = Text()
        styled_name.append(name, style='bold magenta')
        yield from _rows(styled_name, offset, width)
        prefix = '  ' if width >= 8 else ''
        yield from _inset_rows(flags, metadata_offset, width, prefix)
    offset += len(lines[0]) + 1
    for line in lines[1:]:
        yield from _inset_rows(Text(line), offset, width, prefix)
        offset += len(line) + 1


def _parameter_heading(offset, width, name_width):
    text = Text('名称' + ' ' * (name_width - 4) + ' │ 类型 · 要求 · 默认 / 候选；说明', style='dim')
    yield DisplayRow(offset, offset, text, spans=())
    text = Text('─' * name_width + '─┼─' + '─' * (width - name_width - 3), style='dim')
    yield DisplayRow(offset, offset, text, spans=())


def format_help_record(message, value, *, label='', style='', width=80):
    """Keep the logged message authoritative even if metadata is unavailable."""
    width = max(1, width - 2)
    try:
        lines = help_lines(value) if value.get('schema_version') == 1 else []
        if '\n'.join(line.text for line in lines) != message:
            lines = []
    except (AttributeError, KeyError, TypeError, ValueError):
        lines = []
    parameters = value.get('parameters', []) if lines else []
    if not lines:
        lines = [HelpLine(line, 'body') for line in message.split('\n')]
    label_text = _log_text(label, '', style)
    document = label_text.plain + clean_text(message)
    rows, offset, index, parameter_index = [], 0, 0, 0
    name_width = min(24, max([12] + [cell_len(clean_text(item['name'])) for item in parameters]))
    while index < len(lines):
        line = lines[index]
        if line.role == 'parameter' and parameter_index < len(parameters):
            parameter = parameters[parameter_index]
            raw = parameter_text(parameter)
            count = len(raw.split('\n'))
            block = _clean_block(raw, followed_by_newline=index + count < len(lines))
            if parameter_index == 0 and width >= 72:
                rows.extend(_parameter_heading(offset, width, name_width))
            rows.extend(_parameter_rows(parameter, block, offset, width, name_width))
            parameter_index += 1
            index += count
            offset += len(block) + 1
            continue
        text = Text(style=style)
        if index == 0:
            text.append_text(label_text)
        text.append(_clean_block(line.text, followed_by_newline=index + 1 < len(lines)),
                    style=_STYLES.get(line.role, ''))
        local = 0
        for part in text.plain.split('\n'):
            rows.extend(_rows(text[local:local + len(part)], offset, width,
                              header=index == 0 and local == 0))
            local += len(part) + 1
            offset += len(part) + 1
        index += 1
    return FormattedRecord(document, tuple(rows), message, None, source_complete=True)
