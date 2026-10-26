"""Virtual log view with record folding and logical (rather than screen) selections."""

from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass
from queue import Empty, Full, Queue
import threading

from rich.cells import get_character_cell_size
from rich.style import Style
from rich.text import Text
from textual import events
from textual.geometry import Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip

from .log_format import event_metadata, level_label
from .structured_log import COPY_LABEL, COPY_WIDTH, FormattedRecord, clean_text, format_record, preview_record


@dataclass
class LogRecord:
    identity: int
    message: str
    label: str | Text
    style: str
    formatted: FormattedRecord
    expanded: bool = False
    revision: int = 0
    ready: bool = False
    preserve_view_revision: int = -1


@dataclass(frozen=True, order=True)
class TextPosition:
    record: int
    offset: int


class _Formatter:
    """One worker, bounded input AND output. It never calls into Textual."""

    def __init__(self):
        self.jobs = Queue(maxsize=32)
        self.results = Queue(maxsize=32)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name="rc-log-formatter", daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stopped.is_set():
            try:
                identity, revision, message, options = self.jobs.get(timeout=.05)
            except Empty:
                continue
            try:
                result = format_record(message, **options)
            except Exception:
                # Display failures never affect the controller or lose the raw event.
                result = preview_record(message, options['label'], options['style'], options['width'])
            while not self.stopped.is_set():
                try:
                    self.results.put((identity, revision, result), timeout=.05)
                    break
                except Full:
                    pass

    def close(self):
        self.stopped.set()
        self.thread.join(timeout=.2)
        for channel in (self.jobs, self.results):
            while True:
                try:
                    channel.get_nowait()
                except Empty:
                    break


class StructuredLog(ScrollView, can_focus=True):
    """Only visible rows are rendered. One logical record can have many rows.

Selection is owned here because Textual's generic screen selection inserts soft
wrap newlines and loses coordinates when records expand. Mouse capture lets a
drag cross rows and scroll without invoking the fold/copy click actions.
"""

    ALLOW_SELECT = False
    DEFAULT_CSS = """
    StructuredLog { overflow-x: hidden; overflow-y: scroll; background: $surface; color: $text; }
    StructuredLog:focus { border: solid $accent; }
    """

    class PasteToCommand(Message):
        def __init__(self, text):
            super().__init__()
            self.text = text

    def __init__(self, *, max_records=10000, **kwargs):
        super().__init__(**kwargs)
        self.max_records = max_records
        self.records: OrderedDict[int, LogRecord] = OrderedDict()
        self._next_id = 0
        self._ids = []
        self._starts = []
        self._indices = {}
        self._dirty = OrderedDict()
        self._inflight = set()
        self._formatter = None
        self._format_timer = None
        self._closed = False
        self._width = 80
        self._selection: tuple[TextPosition, TextPosition] | None = None
        self._pressed = None
        self._dragged = False
        self._pointer = None
        self._copy_pending = None
        self._batching = False

    def on_mount(self):
        self._formatter = _Formatter()
        self._format_timer = self.set_interval(.03, self._poll_formatting)
        self._resize_layout()

    def on_unmount(self):
        self._closed = True
        if self._format_timer:
            self._format_timer.stop()
        if self._formatter:
            self._formatter.close()
        self.release_mouse()
        self.records.clear()
        self._dirty.clear()
        self._inflight.clear()
        self._ids.clear()
        self._starts.clear()
        self._indices.clear()
        self._selection = self._pressed = None
        self._copy_pending = None

    def write_event(self, event):
        timestamp, level, source = (clean_text(value).replace('\n', ' ') for value in event_metadata(event))
        style = {'ERROR': 'red', 'CRITICAL': 'bold red', 'FATAL': 'bold red', 'WARNING': 'yellow', 'WARN': 'yellow'}.get(level, '')
        level_style = {'INFO': 'bold green', 'DEBUG': 'dim', 'WARN': 'bold yellow',
                       'WARNING': 'bold yellow', 'ERROR': 'bold red', 'CRITICAL': 'bold reverse red',
                       'FATAL': 'bold reverse red', 'TRACE': 'dim'}.get(level, 'bold')
        label = Text()
        if timestamp:
            label.append(f'[{timestamp}] ', style='dim')
        label.append(f'[{level_label(level)}] ', style=level_style)
        label.append(f'[{source}] ', style='bold magenta' if source == 'MCP' else
                     'bold blue' if source.startswith('policy:') else 'bold cyan')
        return self.write(str(event.get('message', '')), label=label, style=style)

    def write_events(self, events):
        """One history/layout update for a controller drain, including eviction."""
        if not events or self._closed:
            return
        anchor, follow = self._anchor(), self._following()
        self._batching = True
        try:
            for event in events:
                self.write_event(event)
        finally:
            self._batching = False
            self._reindex()
            self._restore_view(anchor, follow)
            self.refresh()

    def write(self, message, *, label='', style=''):
        if self._closed:
            return self
        if isinstance(message, Text):
            style, message = str(message.style), message.plain
        message = str(message)
        follow = self._following() if not self._batching else False
        anchor = self._anchor() if not self._batching else None
        identity = self._next_id
        self._next_id += 1
        record = LogRecord(identity, message, label, style,
                           preview_record(message, label, style, self._width))
        self.records[identity] = record
        self._dirty[identity] = None
        removed = False
        while len(self.records) > self.max_records:
            old, _ = self.records.popitem(last=False)
            self._dirty.pop(old, None)
            if self._copy_pending == old:
                self._copy_pending = None
                self.notify('待复制日志已超出保留范围', timeout=2)
            if self._selection and any(point.record == old for point in self._selection):
                self._clear_selection('所选日志已超出保留范围')
            if self._pressed and self._pressed[1][0].record == old:
                self._pressed = None
                self.release_mouse()
            removed = True
        if self._batching:
            return self
        # Appends are O(1); reindex only on eviction or a batch of layout changes.
        if removed:
            self._reindex()
        else:
            self._indices[identity] = len(self._ids)
            self._ids.append(identity)
            self._starts.append(self.virtual_size.height)
            self.virtual_size = Size(self._width, self.virtual_size.height + 1)
        self._restore_view(anchor, follow)
        self.refresh()
        return self

    def _following(self):
        return self.is_vertical_scroll_end and not self._selection and not self._pressed and not self.is_vertical_scrollbar_grabbed

    def _row_at(self, y):
        index = bisect_right(self._starts, y) - 1
        if index < 0 or index >= len(self._ids):
            return None
        record = self.records[self._ids[index]]
        row_index = y - self._starts[index]
        if not 0 <= row_index < len(record.formatted.rows):
            return None
        return record, record.formatted.rows[row_index]

    def _anchor(self):
        found = self._row_at(self.scroll_offset.y)
        return TextPosition(found[0].identity, found[1].start) if found else None

    def _reindex(self):
        self._ids = list(self.records)
        self._starts = []
        self._indices = {}
        height = 0
        for index, record in enumerate(self.records.values()):
            self._indices[record.identity] = index
            self._starts.append(height)
            height += len(record.formatted.rows)
        self.virtual_size = Size(self._width, height)

    def _restore_view(self, anchor, follow=False):
        if follow:
            self.scroll_end(animate=False, immediate=True, x_axis=False)
        elif anchor is not None and anchor.record in self.records:
            record = self.records[anchor.record]
            row = max(0, bisect_right([line.start for line in record.formatted.rows], anchor.offset) - 1)
            self.scroll_to(y=self._starts[self._indices[anchor.record]] + row, animate=False, immediate=True)

    def on_resize(self):
        self._resize_layout()

    def _resize_layout(self):
        width = max(1, self.scrollable_content_region.width)
        if width == self._width:
            return
        self._width = width
        for record in self.records.values():
            record.revision += 1
            record.preserve_view_revision = record.revision
            self._dirty[record.identity] = None
        self._prioritize_visible()

    def _prioritize_visible(self):
        # Make current content useful first after a resize, even with long history.
        visible = []
        for y in range(self.scroll_offset.y, self.scroll_offset.y + self.size.height):
            found = self._row_at(y)
            if found and found[0].identity in self._dirty:
                visible.append(found[0].identity)
        for identity in reversed(visible):
            self._dirty.move_to_end(identity, last=False)

    def _poll_formatting(self):
        if self._closed or not self.is_running or self._formatter is None:
            return
        self._resize_layout()
        anchor, follow = self._anchor(), self._following()
        selected_before = None
        changed = False
        preserve_view = False
        for _ in range(32):
            try:
                identity, revision, result = self._formatter.results.get_nowait()
            except Empty:
                break
            self._inflight.discard((identity, revision))
            record = self.records.get(identity)
            if record is None or record.revision != revision:
                continue
            if not changed and self._selection:
                selected_before = self.selected_text
            preserve_view = preserve_view or record.preserve_view_revision == revision
            record.formatted = result
            record.ready = True
            if self._copy_pending == identity:
                self._copy_pending = None
                self.app.copy_to_clipboard(result.copy_text)
            changed = True
        if changed:
            if selected_before is not None and selected_before != self.selected_text:
                self._clear_selection('日志布局已变化，选区已清除')
            self._reindex()
            self._restore_view(anchor, follow and not preserve_view)
            self.refresh()
        self._prioritize_visible()
        for _ in range(32):
            if not self._dirty:
                break
            identity = next(iter(self._dirty))
            record = self.records[identity]
            options = dict(label=record.label, style=record.style, width=self._width,
                           expanded=record.expanded, parsed=record.formatted.parsed)
            try:
                self._formatter.jobs.put_nowait((identity, record.revision, record.message, options))
            except Full:
                break
            self._dirty.pop(identity)
            self._inflight.add((identity, record.revision))
        self._drag_scroll()

    def toggle(self, identity):
        record = self.records.get(identity)
        if record is None or record.formatted.parsed is None:
            return
        record.expanded = not record.expanded
        record.revision += 1
        record.preserve_view_revision = record.revision
        self._dirty[identity] = None
        self._dirty.move_to_end(identity, last=False)

    @property
    def selected_text(self):
        if self._selection is None:
            return ''
        start, end = sorted(self._selection)
        chunks = []
        for identity, record in self.records.items():
            if not start.record <= identity <= end.record:
                continue
            lower = start.offset if identity == start.record else 0
            upper = end.offset if identity == end.record else len(record.formatted.document)
            text = record.formatted.document
            parts, previous = [], None
            for row in record.formatted.rows:
                begin, finish = max(lower, row.start), min(upper, row.end)
                if begin > finish or row.end < lower or row.start > upper:
                    continue
                if previous is not None:
                    # Omit hidden/truncated preview, retaining real line endings only.
                    parts.append('\n' * text[previous:begin].count('\n'))
                parts.append(text[begin:finish])
                previous = finish
            if parts:
                chunks.append(''.join(parts))
        return '\n'.join(chunks)

    def _clear_selection(self, reason=None):
        self._selection = None
        if reason and self._pressed is not None:
            self._pressed = None
            self.release_mouse()
        if reason and self.is_running:
            self.notify(reason, timeout=2)
        self.refresh()

    def copy_selection(self):
        self._copy_pending = None  # The latest copy gesture owns the clipboard.
        text = self.selected_text
        if text:
            self.app.copy_to_clipboard(text)
        else:
            self.notify('请先拖动选择日志文字', timeout=2)

    def copy_record(self, identity):
        record = self.records.get(identity)
        if record is None:
            return
        if not record.ready:
            self._copy_pending = identity
            if identity in self._dirty:
                self._dirty.move_to_end(identity, last=False)
            self.notify('正在准备可复制内容…', timeout=2)
            return
        self._copy_pending = None
        self.app.copy_to_clipboard(record.formatted.copy_text)

    def render_line(self, y):
        width = max(1, self.scrollable_content_region.width)
        found = self._row_at(y + self.scroll_offset.y)
        if found is None:
            return Strip.blank(width, self.rich_style)
        record, row = found
        content = row.text.copy()
        content.stylize_before(self.rich_style)
        if self._selection:
            start, end = sorted(self._selection)
            if start.record <= record.identity <= end.record:
                lower = start.offset if start.record == record.identity else row.start
                upper = end.offset if end.record == record.identity else row.end
                if upper > row.start and lower < row.end:
                    selection_style = self.screen.get_component_rich_style('screen--selection')
                    if self.screen.get_component_styles('screen--selection').color.a == 0:
                        # A transparent foreground means retain the glyph color.
                        # The full Rich style otherwise blends it into the blue
                        # background, hiding text. Keep the resolved background.
                        selection_style = self.screen.get_component_rich_style(
                            'screen--selection', partial=True) + Style(bgcolor=selection_style.bgcolor)
                    content.stylize(selection_style,
                                    max(0, lower - row.start), min(row.end, upper) - row.start)
        marker = ('▾ ' if record.expanded else '▸ ') if row.header and record.formatted.parsed else '  '
        text = Text(marker, style=self.rich_style)
        room = max(0, width - 2 - (COPY_WIDTH if row.header else 0))
        content.truncate(room, overflow='ellipsis')
        text.append_text(content)
        if row.header:
            text.pad_right(max(0, width - COPY_WIDTH - text.cell_len))
            text.append(COPY_LABEL, style='cyan')
        return Strip(text.render(self.app.console)).crop_extend(0, width, self.rich_style)

    def _hit(self, offset):
        if not self.records:
            return None
        y = min(max(0, offset.y + self.scroll_offset.y), self.virtual_size.height - 1)
        found = self._row_at(y)
        if found is None:
            return None
        record, row = found
        cells = max(0, offset.x - 2)
        char = 0
        for char, value in enumerate(row.text.plain):
            size = get_character_cell_size(value)
            if size > cells:
                break
            cells -= size
        else:
            char = len(row.text.plain)
        position = TextPosition(record.identity, min(row.end, row.start + char))
        action = 'copy' if row.header and offset.x >= self.scrollable_content_region.width - COPY_WIDTH else 'toggle' if row.header else None
        return position, action

    def on_mouse_down(self, event: events.MouseDown):
        if event.button != 1:
            return
        offset = event.get_content_offset_capture(self)
        if offset not in self.scrollable_content_region.size.region:
            return
        if offset.y + self.scroll_offset.y >= self.virtual_size.height:
            event.stop().prevent_default()
            self.focus()
            self._clear_selection()
            return
        hit = self._hit(offset)
        if hit is None:
            return
        event.stop().prevent_default()
        self.focus()
        self.capture_mouse()
        self._pressed = (offset, hit)
        self._pointer = offset
        self._dragged = False
        self._clear_selection()

    def on_mouse_move(self, event: events.MouseMove):
        if self._pressed is None:
            return
        event.stop().prevent_default()
        self._pointer = event.get_content_offset_capture(self)
        if self._pointer != self._pressed[0]:
            self._dragged = True
        self._update_drag()

    def _update_drag(self):
        if self._pressed is None or not self._dragged:
            return
        hit = self._hit(self._pointer)
        if hit:
            self._selection = (self._pressed[1][0], hit[0])
            self.refresh()

    def _drag_scroll(self):
        if self._pressed is None or not self._dragged:
            return
        height = self.scrollable_content_region.height
        dy = -1 if self._pointer.y < 0 else 1 if self._pointer.y >= height else 0
        if dy:
            self.scroll_to(y=self.scroll_y + dy, animate=False, immediate=True)
            self._update_drag()

    def on_mouse_up(self, event: events.MouseUp):
        if event.button != 1 or self._pressed is None:
            return
        event.stop().prevent_default()
        offset = event.get_content_offset_capture(self)
        hit = self._hit(offset)
        pressed = self._pressed
        self._pointer = offset
        self._dragged = self._dragged or offset != pressed[0]
        self._update_drag()
        if not self._dragged and hit == pressed[1]:
            position, action = hit
            if action == 'copy':
                self.copy_record(position.record)
            elif action == 'toggle':
                self.toggle(position.record)
        self._pressed = None
        self.release_mouse()

    def on_click(self, event: events.Click):
        event.stop().prevent_default()  # MouseUp already distinguished click from drag.

    def on_paste(self, event: events.Paste):
        event.stop().prevent_default()
        self.post_message(self.PasteToCommand(event.text))
