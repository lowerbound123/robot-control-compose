"""Literal, responsive tables for the persistent operator statusbar."""

from collections.abc import Mapping

from rich import box
from rich.table import Table
from rich.text import Text
from textual.widgets import Static

_FIELDS = (
    'state', 'policy', 'control_owner', 'in_flight', 'consumed', 'last_action_request',
    'runtime', 'target', 'runtime_error', 'recording_state', 'recording_error',
    'connector_source', 'connector_config',
)


def _display(value):
    return '—' if value is None or value == '' else str(value)


def statusbar_rows(snapshot):
    """Keep every whitelist field, preserving literal paths and full errors."""
    rows = []
    for key in _FIELDS:
        value = snapshot.get(key)
        if key == 'last_action_request':
            request = value if isinstance(value, Mapping) else {}
            text = (f"请求 {_display(request.get('request_id'))}："
                    f"裁剪 {_display(request.get('trim'))}，缓存 {_display(request.get('cached'))}")
        else:
            text = _display(value)
            if key in ('runtime_error', 'recording_error'):
                text = ' '.join(text.splitlines())
                if not text.strip():
                    text = '—'
        rows.append((key, text))
    return tuple(rows)


def statusbar_table(rows, width):
    """Arrange field/value pairs in reading order without clipping either cell."""
    groups = min(3, max(1, width // 38))
    framed = width >= 12
    table = Table(width=width, box=box.SQUARE if framed else None, show_header=False,
                  padding=(0, 1) if framed else 0, border_style='dim', highlight=False)
    # Each pair needs four padding cells and two vertical borders; the final
    # outer border is shared. Reserve useful space for values on narrow screens.
    overhead = 6 * groups + 1 if framed else 0
    pair_width, extra = divmod(max(groups * 2, width - overhead), groups)
    for group in range(groups):
        available = pair_width + (group < extra)
        label_width = min(max(len(key) for key, _ in rows[group::groups]), max(1, available * 3 // 5))
        table.add_column(width=label_width, style='cyan', overflow='fold')
        table.add_column(width=available - label_width, overflow='fold')
    for start in range(0, len(rows), groups):
        cells = [Text(cell, overflow='fold') for pair in rows[start:start + groups] for cell in pair]
        cells.extend(Text('') for _ in range(groups * 2 - len(cells)))
        table.add_row(*cells)
    return table


class StatusBar(Static, can_focus=False):
    """Reflow only when visible values or the actual available width change."""

    def __init__(self, **kwargs):
        super().__init__('Starting…', **kwargs)
        self._rows = ()
        self._table_key = None

    def update_snapshot(self, snapshot):
        self._rows = statusbar_rows(snapshot)
        self._update_table()

    def on_resize(self):
        # Finish the current arrangement before changing intrinsic table height.
        self.call_after_refresh(self._update_table)

    def _update_table(self):
        width = self.content_size.width
        key = (self._rows, width)
        if self._rows and width > 0 and key != self._table_key:
            self._table_key = key
            self.update(statusbar_table(self._rows, width))
