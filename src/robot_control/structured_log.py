"""Pure log parsing/layout. Called by the log widget's single background worker.

Offsets always refer to logical text; wrapping never adds characters to it.
The original message is kept separately for lossless per-record copying.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
import json
import re

from rich.cells import get_character_cell_size
from rich.text import Text

from .literal_text import clean_text


COPY_LABEL = " [复制]"
COPY_WIDTH = 7
_IMAGE_NOTE = re.compile(r"\s*图像内容未显示（\d+ 张）；请查看 images 元数据。\s*\Z")
_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|\btrue\b|\bfalse\b|\bnull\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?')
_COMMAND_PREFIX = re.compile(r"\Apolicy command \[[^\]\r\n]+\] \w+:\s*")
_POLICY_TAG = re.compile(r"\A\s*\[(?!true\]|false\]|null\])[A-Za-z][A-Za-z0-9_.-]*\]\s+")


@dataclass(frozen=True)
class ParsedJson:
    prefix: str
    raw: str
    suffix: str
    compact: str
    pretty: str


@dataclass(frozen=True)
class DisplaySpan:
    """A contiguous source slice at a character offset in a decorated row."""

    display_start: int
    start: int
    end: int


@dataclass(frozen=True)
class DisplayRow:
    start: int
    end: int
    text: Text
    header: bool = False
    # None is the ordinary identity mapping; () is decoration without source.
    spans: tuple[DisplaySpan, ...] | None = None

    def source_at(self, character: int) -> int:
        if self.spans is None:
            return min(self.end, self.start + character)
        previous = self.start
        for span in self.spans:
            if character < span.display_start:
                return previous
            end = span.display_start + span.end - span.start
            if character <= end:
                return span.start + character - span.display_start
            previous = span.end
        return previous

    def selection_ranges(self, lower: int, upper: int):
        spans = self.spans if self.spans is not None else (DisplaySpan(0, self.start, self.end),)
        for span in spans:
            start, end = max(lower, span.start), min(upper, span.end)
            if start < end:
                yield span.display_start + start - span.start, span.display_start + end - span.start


@dataclass(frozen=True)
class FormattedRecord:
    document: str
    rows: tuple[DisplayRow, ...]
    copy_text: str
    parsed: ParsedJson | None
    source_complete: bool = False
    _source_starts: tuple = field(init=False, repr=False, compare=False)
    _source_ends: tuple = field(init=False, repr=False, compare=False)
    _source_rows: tuple = field(init=False, repr=False, compare=False)
    _affinity_starts: tuple = field(init=False, repr=False, compare=False)
    _affinity_rows: tuple = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        # Build once in the formatter worker; UI resize/paint performs logarithmic
        # lookup rather than rescanning a potentially large table.
        spans, affinity = [], {}
        for index, row in enumerate(self.rows):
            affinity.setdefault(row.start, index)
            source = row.spans if row.spans is not None else (DisplaySpan(0, row.start, row.end),)
            spans.extend((span.start, span.end, index) for span in source if span.end > span.start)
        spans.sort()
        positions = sorted(affinity)
        for name, value in (
            ('_source_starts', tuple(item[0] for item in spans)),
            ('_source_ends', tuple(item[1] for item in spans)),
            ('_source_rows', tuple(item[2] for item in spans)),
            ('_affinity_starts', tuple(positions)),
            ('_affinity_rows', tuple(affinity[position] for position in positions)),
        ):
            object.__setattr__(self, name, value)

    def row_for_offset(self, offset):
        """Prefer real content; use stable separator/empty-row affinity in gaps."""
        source = bisect_right(self._source_starts, offset) - 1
        if source >= 0 and offset < self._source_ends[source]:
            return self._source_rows[source]
        affinity = bisect_right(self._affinity_starts, offset) - 1
        if affinity >= 0:
            return self._affinity_rows[affinity]
        return 0


def _reject_constant(value):
    raise ValueError(value)


def parse_json_message(message: str) -> ParsedJson | None:
    decoder = json.JSONDecoder(parse_constant=_reject_constant)
    # Strip only the known command envelope when locating the opening bracket.
    # Once found, a failed outer JSON must never be "repaired" by copying an
    # inner object instead, including when a normal text prefix precedes it.
    # RC prefixes worker stdout with [policy_id]. Only unambiguous identifier
    # tags are skipped: valid JSON such as [true] or [123] remains JSON.
    tag = _POLICY_TAG.match(message)
    offset = tag.end() if tag else 0
    command = _COMMAND_PREFIX.match(message[offset:])
    offset += command.end() if command else 0
    opening = re.search(r"[\[{]", message[offset:])
    if opening is None:
        return None
    start = opening.start() + offset
    try:
        value, end = decoder.raw_decode(message, start)
        if not isinstance(value, (dict, list)):
            return None
        suffix = message[end:]
        if suffix.strip() and not _IMAGE_NOTE.fullmatch(suffix):
            return None  # Multiple JSON fragments are an ambiguous text event.
        return ParsedJson(
            message[:start], message[start:end], suffix,
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False),
        )
    except (ValueError, RecursionError, OverflowError):
        return None


def _styled_json(value: str) -> Text:
    text = Text(value)
    for match in _TOKEN.finditer(value):
        token = match.group()
        if token.startswith('"'):
            color = "bold cyan" if value[match.end():].lstrip().startswith(":") else "green"
        elif token == "null":
            color = "dim magenta"
        elif token in ("true", "false"):
            color = "magenta"
        else:
            color = "yellow"
        text.stylize(color, match.start(), match.end())
    return text


def gutter_width(width):
    return 2 if width >= 4 else 0


def copy_width(content_width):
    return COPY_WIDTH if content_width >= COPY_WIDTH + 2 else 0


def _end_at_width(text: str, start: int, width: int) -> int:
    cells = 0
    for end in range(start, len(text)):
        size = get_character_cell_size(text[end])
        if cells + size > width:
            return max(start + 1, end)
        cells += size
    return len(text)


def _rows(text: Text, offset: int, width: int, *, header=False, fold=False):
    """Character wrapping retains whitespace and exact offsets, including CJK."""
    plain = text.plain
    if fold:
        end = _end_at_width(plain, 0, width)
        if end < len(plain):
            end = _end_at_width(plain, 0, max(0, width - 1)) if width > 1 else 0
            shown = text[:end]
            shown.append("…", style="dim")
        else:
            shown = text.copy()
        yield DisplayRow(offset, offset + end, shown, header)
        return
    start = 0
    first = True
    while start < len(plain) or first:
        # Only the first line has a copy button. Continuations use the full width.
        room = max(1, width - copy_width(width)) if header and first else width
        end = _end_at_width(plain, start, room)
        yield DisplayRow(offset + start, offset + end, text[start:end], header and first)
        start, first = end, False


def _log_text(label, message, style, *, single_line=False):
    text = Text(style=style)
    text.append_text(label if isinstance(label, Text) else Text(clean_text(label)))
    message = clean_text(message)
    text.append(message.replace('\n', ' ') if single_line else message)
    return text


def format_record(message: str, *, label="", style="", width=80, expanded=False,
                  parsed: ParsedJson | None = None, help_document=None,
                  table_document=None, copy_json=None) -> FormattedRecord:
    if help_document is not None:
        from .help_render import format_help_record
        return format_help_record(message, help_document, label=label, style=style, width=width)
    if table_document is not None:
        from .table_render import format_table_record
        try:
            return format_table_record(message, table_document, copy_json, label=label, style=style, width=width)
        except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
            pass  # Optional display metadata never replaces the original message.
    width = max(1, width - gutter_width(width))
    parsed = parsed if parsed is not None else parse_json_message(message)
    rows = []
    if parsed is not None:
        header = _log_text(label, parsed.prefix + parsed.compact + parsed.suffix, style, single_line=True)
        rows.extend(_rows(header, 0, max(1, width - copy_width(width)), header=True, fold=True))
        document = header.plain
        if expanded:
            body = clean_text(parsed.pretty)
            if parsed.suffix.strip():
                body += "\n" + clean_text(parsed.suffix.strip())
            offset = len(header.plain) + 1
            document += "\n" + body
            for line in body.split("\n"):
                rows.extend(_rows(_styled_json(line), offset, width))
                offset += len(line) + 1
        return FormattedRecord(document, tuple(rows), parsed.raw, parsed)
    text = _log_text(label, message, style)
    document = text.plain
    offset = 0
    for index, line in enumerate(document.split("\n")):
        rows.extend(_rows(text[offset:offset + len(line)], offset, width, header=index == 0))
        offset += len(line) + 1
    return FormattedRecord(document, tuple(rows), message, None)


def preview_record(message: str, label: str | Text, style: str, width: int) -> FormattedRecord:
    # Bounded UI work before background formatting; keep the original for copying.
    preview = _log_text(label, message[:512], style, single_line=True)[:512]
    width = max(1, width - gutter_width(width))
    rows = tuple(_rows(preview, 0, max(1, width - copy_width(width)), header=True, fold=True))
    return FormattedRecord(preview.plain, rows, message, None)
