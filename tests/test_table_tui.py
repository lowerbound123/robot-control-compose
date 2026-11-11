"""Rendered table rows retain logical selections, anchors and worker semantics."""
import json
import threading

import pytest
from rich.cells import cell_len

from rc_policy_sdk.tables import table_document_text
from robot_control.log_tui import StructuredLog, TextPosition
from robot_control.tui import CommandInput, RobotControlApp
from test_log_selection_render import rendered_characters
from test_structured_log_tui import point, settled
from test_tui import FakeController


def table_event(*, long=False):
    value = {'schema_version': 1, 'tables': [
        {'title': 'First [bold]', 'columns': ['name', 'details'], 'rows': [
            ['alpha', 'first cell ' + ('中文 path ' * (45 if long else 8))],
            ['beta', 'last\nreal newline']]},
        {'title': 'Second', 'columns': ['key', 'value'], 'rows': [['next', 0], ['off', False]]}]}
    return {'message': table_document_text(value), 'table_document': value,
            'copy_json': '{\n "original" : [false, 0, null, ""]\n}'}


def position(log, record, offset):
    row_index = record.formatted.row_for_offset(offset)
    row = record.formatted.rows[row_index]
    for span in row.spans or ():
        if span.start <= offset <= span.end:
            x = 2 + cell_len(row.text.plain[:span.display_start + offset - span.start])
            y = log._starts[log._indices[record.identity]] + row_index - log.scroll_offset.y
            return point(log, x, y)
    raise AssertionError(f'no visible source at {offset}')


@pytest.mark.asyncio
@pytest.mark.parametrize('reverse', [False, True])
async def test_drag_across_cells_tables_and_log_keeps_source_highlights_and_resize(reverse):
    app = RobotControlApp(FakeController())
    event = table_event()
    async with app.run_test(size=(120, 60)) as pilot:
        log = app.query_one(StructuredLog)
        log.write_event(event)
        log.write('following log')
        await settled(log, pilot)
        record, following = log.records.values()
        lower = record.formatted.document.index('alpha')
        upper = len(following.formatted.document)
        expected = record.formatted.document[lower:] + '\nfollowing log'
        assert record.formatted.source_complete and record.formatted.parsed is None
        start = position(log, record, lower)
        end = point(log, 2 + len('following log'), len(record.formatted.rows))
        source_y = record.formatted.row_for_offset(lower)
        before = rendered_characters(log, source_y)
        if reverse:
            start, end = end, start
        await pilot.mouse_down(log, offset=start)
        await pilot.hover(log, offset=end)
        await pilot.mouse_up(log, offset=end)
        assert log.selected_text == expected
        after = rendered_characters(log, source_y)
        assert ''.join(char for char, _ in before) == ''.join(char for char, _ in after)
        for (char, old), (_, new) in zip(before, after):
            if char == '│':
                assert old.bgcolor == new.bgcolor
        assert any(old.bgcolor != new.bgcolor for (_, old), (_, new) in zip(before, after))
        await pilot.press('ctrl+shift+c')
        assert app.clipboard == expected
        for width in (50, 120):
            await pilot.resize_terminal(width, 60)
            await settled(log, pilot)
            assert log.selected_text == expected
        log.copy_record(record.identity)
        assert app.clipboard == event['copy_json']
        assert app.controller.commands == []


@pytest.mark.asyncio
async def test_scrolled_selection_survives_stacked_layout_append_and_eviction():
    app = RobotControlApp(FakeController())
    event = table_event(long=True)
    event['table_document']['tables'][0]['rows'][0][0] = 'prefix\nsecond line'
    event['message'] = table_document_text(event['table_document'])
    async with app.run_test(size=(50, 24)) as pilot:
        log = app.query_one(StructuredLog)
        log.max_records = 4
        log.write('old')
        log.write_event(event)
        await settled(log, pilot)
        record = list(log.records.values())[1]
        second = record.formatted.document.index('Second')
        assert all(len(row.spans or ()) <= 1 for row in record.formatted.rows if row.start < second)
        log.scroll_home(animate=False, immediate=True)
        lower = record.formatted.document.index('prefix')
        await pilot.mouse_down(log, offset=position(log, record, lower))
        await pilot.hover(log, offset=point(log, 3, 10))
        log.scroll_end(animate=False, immediate=True, x_axis=False)
        await pilot.pause()
        end = len(record.formatted.document)
        await pilot.hover(log, offset=position(log, record, end))
        await pilot.mouse_up(log, offset=position(log, record, end))
        expected = record.formatted.document[lower:]
        assert log.selected_text == expected
        log.write('new 1')
        log.write('new 2')
        log.write('new 3')
        await settled(log, pilot)
        assert log.selected_text == expected and 0 not in log.records
        await pilot.resize_terminal(120, 24)
        await settled(log, pilot)
        assert log.selected_text == expected
        log.write('evict selected')
        await settled(log, pilot)
        assert not log.selected_text


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['continuation', 'stacked', 'border'])
async def test_resize_restores_source_anchor_in_content_or_heading_affinity(target):
    app = RobotControlApp(FakeController())
    event = table_event(long=True)
    if target == 'stacked':
        event['table_document']['tables'][0]['rows'][0][0] = 'prefix\nstacked'
        event['message'] = table_document_text(event['table_document'])
    async with app.run_test(size=(120, 24)) as pilot:
        log = app.query_one(StructuredLog)
        log.write_event(event)
        for index in range(35):
            log.write(f'after {index}')
        await settled(log, pilot)
        record = next(iter(log.records.values()))
        if target == 'border':
            row_index = next(i for i, row in enumerate(record.formatted.rows) if '─┼─' in row.text.plain)
        else:
            offset = record.formatted.document.index('first cell') + (85 if target == 'continuation' else 0)
            row_index = record.formatted.row_for_offset(offset)
        log.scroll_to(y=row_index, animate=False, immediate=True)
        await pilot.pause()
        anchor = log._anchor()
        for width in (50, 120):
            await pilot.resize_terminal(width, 24)
            await settled(log, pilot)
            assert log.scroll_offset.y == record.formatted.row_for_offset(anchor.offset)
        log.write('append while anchored')
        await settled(log, pilot)
        assert log.scroll_offset.y == record.formatted.row_for_offset(anchor.offset)


@pytest.mark.asyncio
async def test_delayed_table_format_preserves_copy_order_input_and_latest_revision(monkeypatch):
    from robot_control import log_tui
    original = log_tui.format_record
    entered, proceed = threading.Event(), threading.Event()
    threads = []
    def delayed(message, **options):
        if options.get('table_document') is not None and not proceed.is_set():
            threads.append(threading.get_ident())
            entered.set()
            assert proceed.wait(8)
        return original(message, **options)
    monkeypatch.setattr(log_tui, 'format_record', delayed)
    app = RobotControlApp(FakeController())
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            log = app.query_one(StructuredLog)
            log.write('selected text')
            await settled(log, pilot)
            event = table_event(long=True)
            log.write_event(event)
            table = list(log.records.values())[-1]
            log.copy_record(table.identity)
            for _ in range(100):
                if entered.is_set():
                    break
                await pilot.pause(.01)
            assert entered.is_set()
            app.query_one(CommandInput).focus()
            await pilot.press('x')
            assert app.query_one(CommandInput).value == 'x'
            await pilot.resize_terminal(50, 40)
            await pilot.resize_terminal(120, 40)
            first = next(iter(log.records.values()))
            log._selection = (TextPosition(first.identity, 0), TextPosition(first.identity, len('selected text')))
            log.copy_selection()
            assert app.clipboard == 'selected text'
            proceed.set()
            await settled(log, pilot)
            assert app.clipboard == 'selected text'
            assert table.formatted.copy_text == event['copy_json']
            assert any('│' in row.text.plain for row in table.formatted.rows)
            assert all(thread != threading.get_ident() for thread in threads)
            log.copy_record(table.identity)
            assert app.clipboard == event['copy_json']
            assert app.query_one(CommandInput).value == 'x'
    finally:
        proceed.set()


@pytest.mark.asyncio
async def test_narrow_table_header_never_loses_source_to_copy_decoration():
    app = RobotControlApp(FakeController())
    event = table_event()
    async with app.run_test(size=(8, 40)) as pilot:
        log = app.query_one(StructuredLog)
        log.write_event(event)
        await settled(log, pilot)
        record = next(iter(log.records.values()))
        log.scroll_home(animate=False, immediate=True)
        await pilot.pause()
        first = record.formatted.rows[0]
        assert first.header and first.text.plain
        assert first.text.plain in log.render_line(0).text
        assert '[复制]' not in log.render_line(0).text


@pytest.mark.asyncio
async def test_copy_requested_before_table_formatting_waits_for_exact_original_json(monkeypatch):
    from robot_control import log_tui
    original = log_tui.format_record
    proceed = threading.Event()
    def delayed(message, **options):
        assert proceed.wait(8)
        return original(message, **options)
    monkeypatch.setattr(log_tui, 'format_record', delayed)
    app = RobotControlApp(FakeController())
    event = table_event()
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            log = app.query_one(StructuredLog)
            log.write_event(event)
            record = next(iter(log.records.values()))
            log.copy_record(record.identity)
            assert app.clipboard == ''
            await pilot.resize_terminal(50, 40)
            proceed.set()
            await settled(log, pilot)
            assert app.clipboard == event['copy_json']
            assert record.formatted.source_complete
    finally:
        proceed.set()
