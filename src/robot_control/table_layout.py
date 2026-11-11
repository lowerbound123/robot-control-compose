"""Source-ordered table layout shared by explicit tables and semantic help.

All ranges refer to the caller's canonical document. Only the final column may
wrap beside another column; if any prefix cannot fit intact the entire table
uses stacked cells. Borders, padding and repeated labels never acquire spans.
"""
from dataclasses import dataclass

from rich.cells import cell_len
from rich.text import Text

from .structured_log import DisplayRow, DisplaySpan, _rows


@dataclass(frozen=True)
class SourceCell:
    start: int
    end: int
    style: str = ''
    styled: Text | None = None


def cell_text(document, cell):
    return cell.styled if cell.styled is not None else Text(document[cell.start:cell.end], style=cell.style)


def cell_rows(document, cell, width, *, prefix=''):
    text = cell_text(document, cell)
    offset, local = cell.start, 0
    # A decorative indent is optional; retaining two cells of content lets CJK
    # remain visible even when an entire terminal is only a few columns wide.
    if cell_len(prefix) + 2 > width:
        prefix = ''
    first = True
    for line in text.plain.split('\n'):
        for row in _rows(text[local:local + len(line)], offset, max(1, width - cell_len(prefix))):
            decorated = Text()
            decorated.append(prefix if first else ' ' * cell_len(prefix), style='dim')
            first = False
            spans = (DisplaySpan(len(decorated), row.start, row.end),) if row.end > row.start else ()
            decorated.append_text(row.text)
            if not text.plain and width - cell_len(prefix) >= 2:
                decorated.append('""', style='dim')
            yield DisplayRow(row.start, row.end, decorated, spans=spans)
        local += len(line) + 1
        offset += len(line) + 1


def layout_table(document, headings, records, width, *, min_width=0,
                 prefix_min=0, labels=True, empty=None):
    """Yield DisplayRows; headings/records contain SourceCells or literal headings.

    Literal headings are decoration (help's existing source has no table header).
    SourceCell headings, including empty headings, belong to the canonical text.
    """
    width = max(1, width)
    names = [document[cell.start:cell.end] if isinstance(cell, SourceCell) else cell for cell in headings]
    columns = len(headings)
    prefixes = [names[:-1]] + [[document[cell.start:cell.end] for cell in row[:-1]] for row in records]
    sizes = [max([prefix_min] + [cell_len(row[index]) if row[index] else 2 for row in prefixes]) for index in range(columns - 1)]
    inset = sum(sizes) + 3 * (columns - 1)
    wide = (width >= min_width and width - inset >= 2
            and not any('\n' in value for row in prefixes for value in row))

    def plain_heading(value):
        return Text(value, style='dim')

    def aligned(cells, *, heading=False):
        leading, spans = Text(), []
        for index, cell in enumerate(cells[:-1]):
            value = cell_text(document, cell) if isinstance(cell, SourceCell) else plain_heading(cell)
            if isinstance(cell, SourceCell) and cell.end > cell.start:
                spans.append(DisplaySpan(len(leading), cell.start, cell.end))
            leading.append_text(value)
            if not value.plain:
                leading.append('""', style='dim')
            leading.append(' ' * (sizes[index] - (value.cell_len if value.plain else 2)) + ' │ ', style='dim')
        final = cells[-1]
        if isinstance(final, SourceCell):
            body = cell_rows(document, final, width - inset)
        else:
            # Literal heading can wrap but has no canonical source range.
            anchor = records[0][0].start if records else 0
            body = (DisplayRow(anchor, anchor, row.text, spans=())
                    for row in _rows(plain_heading(final), anchor, width - inset))
        for index, row in enumerate(body):
            text = leading.copy() if index == 0 else Text()
            if index:
                text.append(''.join(' ' * size + ' │ ' for size in sizes), style='dim')
            mapped = list(spans) if index == 0 else []
            mapped.extend(DisplaySpan(len(text) + span.display_start, span.start, span.end)
                          for span in row.spans or ())
            text.append_text(row.text)
            start = mapped[0].start if mapped else row.start
            end = mapped[-1].end if mapped else row.end
            yield DisplayRow(start, end, text, spans=tuple(mapped))

    if wide:
        yield from aligned(headings, heading=True)
    else:
        for cell in headings:
            if isinstance(cell, SourceCell):
                yield from cell_rows(document, cell, width)
            # Repeated help headings are purely decorative; stacked help already
            # exposes the real parameter/command names in the first cell.
    anchor = (headings[-1].end if isinstance(headings[-1], SourceCell)
              else records[0][0].start if records else 0)
    if width >= 8 and (wide or any(isinstance(cell, SourceCell) for cell in headings)):
        rule = ('─┼─'.join('─' * size for size in [*sizes, width - inset]) if wide else '─' * width)
        yield DisplayRow(anchor, anchor, Text(rule, style='dim'), spans=())
    if not records and empty is not None:
        yield from cell_rows(document, empty, width)
    for row_index, record in enumerate(records):
        if wide:
            yield from aligned(record)
        else:
            if row_index and width >= 8:
                yield DisplayRow(record[0].start, record[0].start, Text('─' * width, style='dim'), spans=())
            for index, cell in enumerate(record):
                prefix = names[index] + ': ' if labels and '\n' not in names[index] else ''
                yield from cell_rows(document, cell, width, prefix=prefix)
