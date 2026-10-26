import asyncio
import base64
import json
import threading

import pytest
from textual import events
from textual.geometry import Offset
from textual.widgets._input import Selection

from robot_control.log_tui import StructuredLog
from robot_control.tui import CommandInput, RobotControlApp
from test_tui import FakeController


async def settled(log, pilot):
    for _ in range(200):
        await pilot.pause(.01)
        if not log._dirty and not log._inflight:
            return
    raise AssertionError('background log formatter did not settle')


def point(log, x=3, y=0):
    return (log.gutter.left + x, log.gutter.top + y)


@pytest.mark.asyncio
async def test_click_expands_and_copies_original_json_without_toggling():
    controller = FakeController()
    raw = '{\n "中文": [1, true, null], "text": "' + '内容' * 50 + '"\n}'
    controller.events.put(dict(time='12:00', level='INFO', message='MCP state: ' + raw))
    app = RobotControlApp(controller)
    async with app.run_test(size=(90, 32)) as pilot:
        log = app.query_one(StructuredLog)
        await settled(log, pilot)
        record = next(iter(log.records.values()))
        assert len(record.formatted.rows) == 1
        await pilot.click(log, offset=point(log))
        await settled(log, pilot)
        assert record.expanded and len(record.formatted.rows) > 5
        await pilot.click(log, offset=point(log, log.scrollable_content_region.width - 3))
        assert app.clipboard == raw
        assert record.expanded
        await pilot.click(log, offset=point(log))
        await settled(log, pilot)
        assert not record.expanded and len(record.formatted.rows) == 1


@pytest.mark.asyncio
async def test_drag_selection_crosses_records_without_soft_wrap_newlines_or_toggle():
    app = RobotControlApp(FakeController())
    async with app.run_test(size=(50, 32)) as pilot:
        log = app.query_one(StructuredLog)
        message = '中文abcdefghij' * 7
        log.write(message)
        log.write('last line')
        await settled(log, pilot)
        first, last = log.records.values()
        end_y = len(first.formatted.rows)
        await pilot.mouse_down(log, offset=point(log, 2, 0))
        await pilot.hover(log, offset=point(log, 2 + len('last line'), end_y))
        await pilot.mouse_up(log, offset=point(log, 2 + len('last line'), end_y))
        await pilot.press('ctrl+shift+c')
        assert app.clipboard == message + '\nlast line'
        assert not app.controller.commands
        before = log.selected_text
        log.write('arrived while selected')
        await settled(log, pilot)
        assert log.selected_text == before
        await pilot.resize_terminal(65, 35)
        await settled(log, pilot)
        assert log.selected_text == before


@pytest.mark.asyncio
async def test_expanded_body_selection_copies_valid_json_and_collapse_clears_hidden_selection():
    app = RobotControlApp(FakeController())
    async with app.run_test(size=(42, 35)) as pilot:
        log = app.query_one(StructuredLog)
        log.write('{"long":"' + 'abc中文' * 12 + '","n":1}')
        await settled(log, pilot)
        await pilot.click(log, offset=point(log))
        await settled(log, pilot)
        record = next(iter(log.records.values()))
        end_y = len(record.formatted.rows) - 1
        await pilot.mouse_down(log, offset=point(log, 2, 1))
        await pilot.hover(log, offset=point(log, 3, end_y))
        await pilot.mouse_up(log, offset=point(log, 3, end_y))
        assert record.expanded
        await pilot.press('ctrl+shift+c')
        assert json.loads(app.clipboard) == {'long': 'abc中文' * 12, 'n': 1}
        log.toggle(record.identity)
        await settled(log, pilot)
        assert not log.selected_text


@pytest.mark.asyncio
async def test_log_and_command_paste_preserve_all_lines_without_submission():
    app = RobotControlApp(FakeController())
    async with app.run_test() as pilot:
        command = app.query_one(CommandInput)
        command.value = 'set OLD tail'
        command.selection = Selection(4, 7)
        log = app.query_one(StructuredLog)
        log.focus()
        log.post_message(events.Paste('"a\r\nb\rc\nd"'))
        await pilot.pause()
        assert command.value == 'set "a b c d" tail'
        assert app.focused is command and app.controller.commands == []
        command.value = ''
        command.post_message(events.Paste('one\ntwo\nthree'))
        await pilot.pause()
        assert command.value == 'one two three'
        assert app.controller.commands == []


@pytest.mark.asyncio
async def test_osc52_copy_and_exit_keys_still_use_cleanup_flow():
    app = RobotControlApp(FakeController())
    async with app.run_test() as pilot:
        log = app.query_one(StructuredLog)
        log.write('你好 "quoted"')
        await settled(log, pilot)
        writes = []
        driver = app._driver
        original = driver.write
        driver.write = writes.append
        try:
            await pilot.click(log, offset=point(log, log.scrollable_content_region.width - 3))
        finally:
            driver.write = original
        escapes = [value for value in writes if value.startswith('\x1b]52;c;')]
        assert base64.b64decode(escapes[-1][7:-1]).decode() == '你好 "quoted"'
        await pilot.press('ctrl+shift+c')  # No selection must not exit.
        assert not app.controller.commands
        await pilot.press('ctrl+c')
        assert app.controller.commands == ['exit']
        assert app.is_running


@pytest.mark.asyncio
async def test_history_eviction_scroll_anchor_and_worker_cleanup():
    app = RobotControlApp(FakeController())
    async with app.run_test(size=(60, 25)) as pilot:
        log = app.query_one(StructuredLog)
        log.max_records = 80
        for index in range(70):
            log.write(f'event {index}')
        await settled(log, pilot)
        assert log.is_vertical_scroll_end
        log.scroll_to(y=15, animate=False, immediate=True)
        anchor = log._row_at(log.scroll_offset.y)[0].identity
        for index in range(70, 90):
            log.write(f'event {index}')
        await settled(log, pilot)
        assert len(log.records) == 80
        assert log._row_at(log.scroll_offset.y)[0].identity == anchor
        assert not log.is_vertical_scroll_end
        thread = log._formatter.thread
    await asyncio.sleep(.1)
    assert not thread.is_alive()


@pytest.mark.asyncio
async def test_copy_during_pending_format_waits_for_json_payload(monkeypatch):
    from robot_control import log_tui
    original = log_tui.format_record
    entered, proceed = threading.Event(), threading.Event()
    def delayed(*args, **kwargs):
        entered.set()
        assert proceed.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(log_tui, 'format_record', delayed)
    app = RobotControlApp(FakeController())
    try:
        async with app.run_test() as pilot:
            log = app.query_one(StructuredLog)
            log.write('MCP state: {"v": 2}')
            for _ in range(100):
                if entered.is_set(): break
                await pilot.pause(.01)
            log.copy_record(next(iter(log.records)))
            assert app.clipboard == ''
            proceed.set()
            await settled(log, pilot)
            assert app.clipboard == '{"v": 2}'
    finally:
        proceed.set()


@pytest.mark.asyncio
async def test_header_drag_selects_preview_without_button_marker_or_toggle():
    app = RobotControlApp(FakeController())
    async with app.run_test(size=(40, 24)) as pilot:
        log = app.query_one(StructuredLog)
        log.write('MCP: {"long":"' + 'x' * 100 + '"}')
        await settled(log, pilot)
        record = next(iter(log.records.values()))
        await pilot.mouse_down(log, offset=point(log, 0))
        await pilot.hover(log, offset=point(log, log.scrollable_content_region.width - 1))
        await pilot.mouse_up(log, offset=point(log, log.scrollable_content_region.width - 1))
        await pilot.press('ctrl+shift+c')
        assert app.clipboard.startswith('MCP: {"long":')
        assert len(app.clipboard) < 35
        assert '复制' not in app.clipboard and '…' not in app.clipboard
        assert not record.expanded


@pytest.mark.asyncio
async def test_formatter_congestion_eviction_and_late_completion_do_not_block_ui(monkeypatch):
    from robot_control import log_tui
    original = log_tui.format_record
    entered, proceed = threading.Event(), threading.Event()
    def delayed(*args, **kwargs):
        entered.set()
        assert proceed.wait(8)
        return original(*args, **kwargs)
    monkeypatch.setattr(log_tui, 'format_record', delayed)
    app = RobotControlApp(FakeController())
    try:
        async with app.run_test() as pilot:
            log = app.query_one(StructuredLog)
            log.max_records = 64
            for index in range(100): log.write(f'old {index}')
            await pilot.pause(.1)
            assert entered.is_set()
            for index in range(100): log.write(f'new {index}')
            await pilot.press('ctrl+q')
            assert app.controller.commands == ['exit']
            assert len(log.records) == 64
            assert log._formatter.jobs.qsize() <= 32
            assert all(record.message.startswith('new ') for record in log.records.values())
            # Shutdown while the worker is still busy. No call_from_thread can
            # target removed widgets; the formatter only writes to its own queue.
            thread = log._formatter.thread
        proceed.set()
        for _ in range(100):
            if not thread.is_alive(): break
            await asyncio.sleep(.01)
        assert not thread.is_alive()
        assert not log.records and not log._inflight and not log._dirty
        assert log._formatter.results.empty()
    finally:
        proceed.set()


@pytest.mark.asyncio
async def test_enhanced_terminal_copy_key_does_not_become_safe_exit():
    from textual._xterm_parser import XTermParser
    app = RobotControlApp(FakeController())
    async with app.run_test() as pilot:
        app.query_one(StructuredLog).focus()
        parser = XTermParser(debug=False)
        for event in parser.feed('\x1b[99;6u'):
            app.post_message(event)
        await pilot.pause()
        assert not app.controller.commands
        for event in parser.feed('\x1b[99;5u'):
            app.post_message(event)
        await pilot.pause()
        assert app.controller.commands == ['exit']


@pytest.mark.asyncio
async def test_large_batch_retains_whole_events_and_keeps_input_accessible():
    app = RobotControlApp(FakeController())
    async with app.run_test(size=(32, 18)) as pilot:
        log = app.query_one(StructuredLog)
        log.write_events([dict(time='', level='INFO', message=f'event {index}') for index in range(10020)])
        assert len(log.records) == 10000
        assert next(iter(log.records.values())).message == 'event 20'
        await pilot.press('ctrl+q')
        assert app.controller.commands == ['exit']
        assert app.query_one(CommandInput).region.bottom <= app.size.height


@pytest.mark.asyncio
async def test_copy_selection_supersedes_earlier_pending_record_copy(monkeypatch):
    from robot_control import log_tui
    original = log_tui.format_record
    proceed = threading.Event()
    def delayed(message, **kwargs):
        if message.startswith('MCP'):
            assert proceed.wait(5)
        return original(message, **kwargs)
    monkeypatch.setattr(log_tui, 'format_record', delayed)
    app = RobotControlApp(FakeController())
    try:
        async with app.run_test() as pilot:
            log = app.query_one(StructuredLog)
            log.write('selected')
            await settled(log, pilot)
            log.write('MCP: {"old":1}')
            log.copy_record(next(reversed(log.records)))
            await pilot.mouse_down(log, offset=point(log, 2))
            await pilot.hover(log, offset=point(log, 10))
            await pilot.mouse_up(log, offset=point(log, 10))
            await pilot.press('ctrl+shift+c')
            assert app.clipboard == 'selected'
            proceed.set()
            await settled(log, pilot)
            assert app.clipboard == 'selected'
    finally:
        proceed.set()


@pytest.mark.asyncio
async def test_expanding_bottom_record_preserves_top_log_instead_of_following_new_rows():
    app = RobotControlApp(FakeController())
    async with app.run_test(size=(60, 25)) as pilot:
        log = app.query_one(StructuredLog)
        log.write_events([dict(message=f'event {i}') for i in range(12)])
        log.write(json.dumps({f'field{i}': i for i in range(60)}))
        await settled(log, pilot)
        assert log.is_vertical_scroll_end
        anchor = log._anchor()
        identity = next(reversed(log.records))
        log.toggle(identity)
        await settled(log, pilot)
        assert log._anchor() == anchor
        assert not log.is_vertical_scroll_end
        assert log._starts[log._indices[identity]] - log.scroll_offset.y < log.scrollable_content_region.height


@pytest.mark.asyncio
async def test_resize_keeps_top_log_even_when_previous_view_was_at_bottom():
    app = RobotControlApp(FakeController())
    async with app.run_test(size=(110, 30)) as pilot:
        log = app.query_one(StructuredLog)
        for i in range(12): log.write(str(i) + '中文' * 20)
        await settled(log, pilot)
        assert log.is_vertical_scroll_end
        anchor = log._anchor()
        await pilot.resize_terminal(40, 30)
        await settled(log, pilot)
        assert log._anchor() == anchor


@pytest.mark.asyncio
async def test_idle_refresh_does_not_rescan_selection(monkeypatch):
    app = RobotControlApp(FakeController())
    async with app.run_test() as pilot:
        log = app.query_one(StructuredLog)
        log.write('selected text')
        await settled(log, pilot)
        await pilot.mouse_down(log, offset=point(log, 2))
        await pilot.hover(log, offset=point(log, 10))
        await pilot.mouse_up(log, offset=point(log, 10))
        assert log.selected_text
        # Work-budget guard: extracting a large selection walks all selected
        # records. Idle timer ticks must not do that work at all.
        original = StructuredLog.selected_text.fget
        reads = []
        def counted(widget):
            reads.append(True)
            return original(widget)
        monkeypatch.setattr(StructuredLog, 'selected_text', property(counted))
        for _ in range(3): log._poll_formatting()
        assert not reads


@pytest.mark.asyncio
async def test_clicking_empty_log_background_does_not_toggle_or_copy_last_record():
    app = RobotControlApp(FakeController())
    async with app.run_test() as pilot:
        log = app.query_one(StructuredLog)
        log.write('{"last":true}')
        await settled(log, pilot)
        record = next(iter(log.records.values()))
        await pilot.click(log, offset=point(log, 3, 4))
        await settled(log, pilot)
        assert not record.expanded
        await pilot.click(log, offset=point(log, log.scrollable_content_region.width - 3, 5))
        assert app.clipboard == ''


@pytest.mark.asyncio
async def test_invalidated_selection_cancels_drag_after_pending_json_is_formatted(monkeypatch):
    from robot_control import log_tui
    original = log_tui.format_record
    proceed = threading.Event()
    def delayed(*args, **kwargs):
        assert proceed.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(log_tui, 'format_record', delayed)
    app = RobotControlApp(FakeController())
    try:
        async with app.run_test() as pilot:
            log = app.query_one(StructuredLog)
            log.write('MCP: {       "x": 1}')
            await pilot.mouse_down(log, offset=point(log, 2))
            await pilot.hover(log, offset=point(log, 16))
            assert log.selected_text
            proceed.set()
            await settled(log, pilot)
            assert not log.selected_text
            await pilot.hover(log, offset=point(log, 18))
            await pilot.mouse_up(log, offset=point(log, 18))
            await pilot.press('ctrl+shift+c')
            assert app.clipboard == ''
    finally:
        proceed.set()
