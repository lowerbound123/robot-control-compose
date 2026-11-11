"""Semantic help using the common source-ordered table layout."""
from rich.text import Text

from .help_document import HelpLine, help_lines, parameter_text
from .literal_text import clean_boundaries
from .structured_log import FormattedRecord, _log_text, _rows, gutter_width
from .table_layout import SourceCell, layout_table


_STYLES = {'title': 'bold bright_cyan', 'section': 'bold bright_blue',
           'usage': 'cyan', 'example': 'green'}


def format_help_record(message, value, *, label='', style='', width=80):
    """Retain help_text/copy exactly; metadata only supplies cell source ranges."""
    width = max(1, width - gutter_width(width))
    try:
        lines = help_lines(value) if value.get('schema_version') == 1 else []
        if '\n'.join(line.text for line in lines) != message:
            lines = []
    except (AttributeError, KeyError, TypeError, ValueError):
        lines = []
    parameters = value.get('parameters', []) if lines else []
    groups = {}
    for command in value.get('commands', []) if lines else []:
        groups.setdefault(command['group'], []).append(command)
    commands = [command for group in groups.values() for command in group]
    if not lines:
        lines = [HelpLine(line, 'body') for line in message.split('\n')]

    # Derive ranges from the semantic model, never by searching/parsing displayed
    # fragments. One cleaner sees all separators and embedded tabs/CRLF.
    starts, raw_offset = [], 0
    for line in lines:
        starts.append(raw_offset)
        raw_offset += len(line.text) + 1
    boundaries = set(starts) | {min(len(message), start + len(line.text)) for start, line in zip(starts, lines)}
    blocks, index, parameter_index, command_index = [], 0, 0, 0
    while index < len(lines):
        line = lines[index]
        if line.role in ('parameter', 'catalog'):
            role, records = line.role, []
            while index < len(lines) and lines[index].role == role:
                if role == 'parameter' and parameter_index < len(parameters):
                    item = parameters[parameter_index]
                    raw = parameter_text(item)
                    name, gap = item['name'], 2
                    parameter_index += 1
                elif role == 'catalog' and command_index < len(commands):
                    item = commands[command_index]
                    name = item['command']
                    gap = 3 if item['summary'] else 0
                    raw = name + (' — ' + item['summary'] if item['summary'] else '')
                    command_index += 1
                else:
                    break
                start = starts[index]
                end = start + len(raw)
                ranges = ((start, start + len(name)), (start + len(name) + gap, end))
                boundaries.update(point for cell in ranges for point in cell)
                records.append(ranges)
                index += len(raw.split('\n'))
            if records:
                blocks.append((role, records))
                continue
        blocks.append(('line', (index, starts[index], starts[index] + len(line.text))))
        index += 1
    plain, mapping = clean_boundaries(message, boundaries)
    label_text = _log_text(label, '', style)
    shift = len(label_text)
    document = label_text.plain + plain
    rows = []
    for kind, block in blocks:
        if kind == 'line':
            index, lower, upper = block
            start, end = shift + mapping[lower], shift + mapping[upper]
            text = Text(document[start:end], style=_STYLES.get(lines[index].role, style))
            if index == 0:
                text = label_text + text
                start = 0
            local = 0
            for part in text.plain.split('\n'):
                rows.extend(_rows(text[local:local + len(part)], start + local, width,
                                  header=index == 0 and local == 0))
                local += len(part) + 1
            continue
        records = []
        for name_range, value_range in block:
            lower, upper = (shift + mapping[point] for point in name_range)
            name = SourceCell(lower, upper, 'bold magenta' if kind == 'parameter' else 'cyan')
            lower, upper = (shift + mapping[point] for point in value_range)
            text = Text(document[lower:upper])
            if kind == 'parameter':
                text.stylize('cyan', 0, text.plain.find('\n') if '\n' in text.plain else len(text))
            records.append([name, SourceCell(lower, upper, styled=text)])
        headings = ['名称', '类型 · 要求 · 默认 / 候选；说明'] if kind == 'parameter' else ['命令', '说明']
        rows.extend(layout_table(document, headings, records, width, min_width=72, prefix_min=12, labels=False))
    return FormattedRecord(document, tuple(rows), message, None, source_complete=True)
