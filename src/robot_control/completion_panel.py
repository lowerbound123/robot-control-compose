"""A non-focusable, bounded completion grid with a readable selected-item detail."""
from __future__ import annotations

import math
from rich.cells import cell_len
from rich.text import Text
from textual.containers import Vertical
from textual.widgets import Static
from .policy_monitor_tui import literal


class CompletionPanel(Vertical):
    can_focus = False
    DEFAULT_CSS = """
    CompletionPanel { height: auto; display: none; padding: 0 1; background: $panel; }
    CompletionPanel > #completion-grid { height: auto; }
    CompletionPanel > #completion-detail { height: auto; max-height: 3; overflow-y: auto; color: $text-muted; }
    """

    def __init__(self):
        super().__init__(id='completion-panel')
        self.columns = 1
        self.top_row = 0
        self.visible_rows = 0
        self._state = None

    def compose(self):
        yield Static(id='completion-grid', markup=False)
        yield Static(id='completion-detail', markup=False)

    def present(self, result, selected, menu_open):
        self._state = result, selected, menu_open
        self._render_state()

    def on_resize(self):
        self._render_state()

    def _render_state(self):
        if self._state is None or not self.is_mounted:
            return
        result, selected, menu_open = self._state
        items = result.items if result is not None else ()
        width = max(1, self.content_size.width or self.app.size.width - 2)
        cell_width = min(width, max(12, min(34, max((cell_len(literal(item.label)) for item in items), default=10) + 3)))
        self.columns = max(1, width // cell_width)
        count = math.ceil(len(items) / self.columns)
        max_rows = min(6, max(1, self.app.size.height // 3 - 2))
        self.visible_rows = min(count, max_rows) if menu_open else 0
        grid = Text()
        if menu_open and items:
            selected = min(max(0, selected), len(items) - 1)
            row = selected // self.columns
            self.top_row = min(self.top_row, max(0, count - max_rows))
            if row < self.top_row:
                self.top_row = row
            elif row >= self.top_row + max_rows:
                self.top_row = row - max_rows + 1
            for row_index in range(self.top_row, min(count, self.top_row + max_rows)):
                if grid:
                    grid.append('\n')
                for column in range(self.columns):
                    index = row_index * self.columns + column
                    if index >= len(items):
                        break
                    label = Text(('› ' if index == selected else '  ') + literal(items[index].label).replace('\n', '\\n').replace('\t', '\\t'))
                    label.truncate(max(1, cell_width - 1), overflow='ellipsis', pad=True)
                    label.append(' ')
                    if index == selected:
                        label.stylize('bold reverse')
                    grid.append_text(label)
            item = items[selected]
            detail = Text(literal(item.label), style='bold')
            if item.kind:
                detail.append(' · ' + literal(item.kind))
            if item.help and not (result is not None and result.hint.endswith(item.help)):
                detail.append('\n' + literal(item.help))
            if count > max_rows:
                detail.append(f'  [{selected + 1}/{len(items)}]')
        else:
            self.top_row = 0
            detail = Text('无匹配候选' if menu_open else '')
        if result is not None:
            notes = [value for value in (result.hint, result.lint) if value]
            if notes:
                if detail:
                    detail.append('\n')
                detail.append(' · '.join(literal(note) for note in notes), style='yellow' if result.lint else '')
        self.query_one('#completion-grid', Static).update(grid)
        self.query_one('#completion-grid').display = bool(grid)
        self.query_one('#completion-detail', Static).update(detail)
        self.display = menu_open or bool(detail)

    def move(self, selected, size, direction):
        if not size:
            return 0
        columns = max(1, self.columns)
        if direction in ('previous', 'next') or columns == 1:
            delta = -1 if direction in ('previous', 'left', 'up') else 1
            return (selected + delta) % size
        row, column = divmod(selected, columns)
        if direction in ('left', 'right'):
            width = min(columns, size - row * columns)
            return row * columns + (column + (-1 if direction == 'left' else 1)) % width
        rows = math.ceil(size / columns)
        row = (row + (-1 if direction == 'up' else 1)) % rows
        return min(row * columns + column, size - 1)
