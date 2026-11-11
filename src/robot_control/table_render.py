"""Explicit table metadata to a stable logical document and bounded-width rows."""
import json
import math

from rich.text import Text
from rc_policy_sdk.tables import normalize_table_document, table_cell_text

from .literal_text import clean_boundaries
from .structured_log import DisplayRow, DisplaySpan, FormattedRecord, _log_text, _reject_constant, _rows, gutter_width
from .table_layout import SourceCell, cell_rows, layout_table


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('original table result must be finite JSON')
    return number


def format_table_record(message, value, copy_json, *, label='', style='', width=80):
    if not isinstance(copy_json, str):
        raise ValueError('table copy_json must be provided by the producer')
    json.loads(copy_json, parse_constant=_reject_constant, parse_float=_finite_float)
    value = normalize_table_document(value)
    chunks, tables, length, boundaries = [], [], 0, {0}

    def add(text):
        nonlocal length
        start = length
        chunks.append(text)
        length += len(text)
        boundaries.update((start, length))
        return (start, length)

    for index, table in enumerate(value['tables']):
        if index:
            add('\n\n')
        title = add(table['title']) if table['title'] else None
        if title is not None:
            add('\n')
        headings, records = [], []
        for index, column in enumerate(table['columns']):
            if index:
                add('\t')
            headings.append(add(column))
        for values in table['rows']:
            add('\n')
            cells = []
            for index, cell in enumerate(values):
                if index:
                    add('\t')
                cells.append(add(table_cell_text(cell)))
            records.append(cells)
        empty = None
        if not records:
            add('\n')
            empty = add('无记录')
        tables.append((title, headings, records, empty))
    if ''.join(chunks) != message:
        raise ValueError('table metadata does not match the original message')
    plain, offsets = clean_boundaries(message, boundaries)
    label_text = _log_text(label, '', style)
    shift = len(label_text)
    document = label_text.plain + plain
    width = max(1, width - gutter_width(width))
    rows = []

    def cell(raw, style=''):
        return SourceCell(shift + offsets[raw[0]], shift + offsets[raw[1]], style)

    # Keep the copy affordance on its own metadata row, so it can never truncate
    # a table heading or its first cell. The label remains part of the source.
    if shift:
        rows.extend(_rows(label_text, 0, width, header=True))
    else:
        rows.append(DisplayRow(0, 0, Text(), header=True, spans=()))
    for index, (title, headings, records, empty) in enumerate(tables):
        if index:
            anchor = shift + offsets[(title or headings[0])[0]]
            rows.append(DisplayRow(anchor, anchor, Text(), spans=()))
        if title is not None:
            rows.extend(cell_rows(document, cell(title, 'bold bright_cyan'), width))
        rows.extend(layout_table(document, [cell(raw, 'bold cyan') for raw in headings],
            [[cell(raw) for raw in record] for record in records], width,
            empty=cell(empty, 'dim') if empty else None))
    return FormattedRecord(document, tuple(rows), copy_json, None, source_complete=True)
