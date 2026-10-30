import io
import json

import pytest

from robot_control.cli import format_event
from robot_control.graphics import GraphicsSupport
from robot_control.log_tui import StructuredLog
from robot_control.tui import CommandInput, RobotControlApp
from robot_control.tui_panels import CameraPanel, MonitorPanel
from test_controller import setup_controller as setup_controller
from test_structured_log_tui import settled
from test_tui import FakeController


@pytest.mark.parametrize('epoch, expected', [
    (0, '1970-01-01T08:00:00.000'),
    (57599.125, '1970-01-01T23:59:59.125'),
    (57600, '1970-01-02T00:00:00.000'),
])
def test_headless_timestamp_has_fixed_utc8_date_and_milliseconds(epoch, expected):
    assert format_event({'time': epoch, 'level': 'info', 'message': 'ready'}) == f'[{expected}] [INFO] [RC] ready'


@pytest.mark.parametrize('level, label', [('info', 'INFO'), ('warn', 'WARN'),
    ('warning', 'WARN'), ('error', 'ERRO'), ('debug', 'DBUG'), ('critical', 'CRIT'),
    ('fatal', 'FATL'), ('trace', 'TRCE')])
def test_rendered_log_levels_have_four_letters_and_no_timezone_suffix(level, label):
    event = {'time': '2026-10-26T14:30:00.125+08:00', 'level': level, 'message': 'ready'}
    assert format_event(event) == f'[2026-10-26T14:30:00.125] [{label}] [RC] ready'


@pytest.mark.parametrize('message', ['[7hold] ready', '[123] ready'])
def test_digit_leading_policy_ids_keep_their_source_label(message):
    identity = message[1:message.index(']')]
    assert format_event({'level': 'info', 'message': message}) == f'[INFO] [policy:{identity}] {message}'


def test_log_queue_history_and_file_share_iso_timestamp(setup_controller, monkeypatch):
    controller, _, _ = setup_controller
    entries = []
    monkeypatch.setattr(controller._recorder, 'log', entries.append)
    monkeypatch.setattr('robot_control.controller.time.time', lambda: 57600.125)
    try:
        controller._emit('message')
        event = controller.events.get_nowait()
        assert event['time'] == '1970-01-02T00:00:00.125+08:00'
        assert controller.read_events()['events'][-1] == event
        assert {key:entries[0][key] for key in event} == event
    finally:
        controller.close_unstarted()


@pytest.mark.asyncio
async def test_monitor_fits_all_fourteen_narrower_cells_and_scrolls_only_when_needed():
    app = RobotControlApp(FakeController())
    async with app.run_test(size=(121, 30)) as pilot:
        panel = app.query_one(MonitorPanel)
        panel.display = True
        panel.update_snapshot({'qpos': [-123456789.123] * 14, 'epos': [1e100] * 14})
        await pilot.pause()
        assert panel.max_scroll_x == 0
        assert not panel.show_horizontal_scrollbar
        lines = panel.query_one('#monitor-values').content.plain.splitlines()
        assert 'R.G' in lines[0]
        # All 14 fixed-width cells keep a non-zero value, including when the
        # value needs scientific notation instead of plain decimal text.
        for line in (lines[1], lines[3]):
            cells = [line[offset:offset + 8] for offset in range(7, len(line), 8)]
            assert len(cells) == 14
            assert all(float(cell) != 0 for cell in cells)
        await pilot.resize_terminal(80, 30)
        assert panel.show_horizontal_scrollbar and panel.max_scroll_x > 0
        panel.scroll_end(animate=False, immediate=True)
        await pilot.resize_terminal(160, 30)
        assert not panel.show_horizontal_scrollbar and panel.scroll_x == 0


@pytest.mark.asyncio
async def test_camera_border_drag_resizes_without_hiding_command_or_losing_capture():
    app = RobotControlApp(FakeController(), graphics=GraphicsSupport('kitty'))
    async with app.run_test(size=(140, 40)) as pilot:
        panel = app.query_one(CameraPanel)
        panel.display = True
        await pilot.pause()
        original = panel.region.height
        edge = panel.region.y
        await pilot.mouse_down(offset=(50, edge))
        await pilot.hover(offset=(50, edge - 5))
        await pilot.mouse_up(offset=(50, edge - 5))
        assert panel.region.height == original + 5
        assert app.mouse_captured is None
        edge = panel.region.y
        await pilot.mouse_down(offset=(50, edge))
        await pilot.hover(offset=(50, 0))
        await pilot.mouse_up(offset=(50, 0))
        assert app.query_one('#log-area').size.height >= 2
        assert panel.region.bottom <= app.query_one('#command-area').region.y
        await pilot.resize_terminal(100, 24)
        assert app.query_one(CommandInput).region.bottom <= 24
        assert panel.region.bottom <= app.query_one('#command-area').region.y
        app.query_one(MonitorPanel).display = True
        await pilot.pause(.1)
        assert app.query_one('#log-area').size.height >= 2
        assert panel.region.bottom <= app.query_one('#command-area').region.y
        edge = panel.region.y
        await pilot.mouse_down(offset=(50, edge))
        await pilot.hover(offset=(50, 23))
        await pilot.mouse_up(offset=(50, 23))
        assert panel.region.height == 4


@pytest.mark.asyncio
async def test_camera_only_top_edge_starts_drag_and_hiding_releases_mouse():
    app = RobotControlApp(FakeController(), graphics=GraphicsSupport('kitty'))
    async with app.run_test(size=(140, 40)) as pilot:
        panel = app.query_one(CameraPanel)
        panel.display = True
        await pilot.pause()
        original = panel.region.height
        await pilot.mouse_down(offset=(50, panel.region.y + 2))
        await pilot.hover(offset=(50, panel.region.y - 3))
        await pilot.mouse_up(offset=(50, panel.region.y - 3))
        assert panel.region.height == original
        await pilot.mouse_down(offset=(50, panel.region.y))
        assert app.mouse_captured is panel
        panel.display = False
        # Hide is posted by the next compositor reflow, after CSS display changes.
        for _ in range(30):
            await pilot.pause(.01)
            if app.mouse_captured is None:
                break
        assert app.mouse_captured is None
        await pilot.mouse_up(offset=(50, 2))
        panel.display = True
        await pilot.hover(offset=(50, 3))
        assert panel.region.height == original


@pytest.mark.asyncio
async def test_log_timestamp_level_and_sender_have_distinct_styles_without_changing_copy():
    app = RobotControlApp(FakeController())
    async with app.run_test(size=(200, 30)) as pilot:
        log = app.query_one(StructuredLog)
        log.write_events([
            {'time': 0, 'level': 'info', 'message': 'ready'},
            {'time': 0, 'level': 'warn', 'message': '[hold] slow'},
            {'time': 0, 'level': 'error', 'message': 'MCP state: {"x":1}'},
        ])
        await settled(log, pilot)
        rc, policy, mcp = log.records.values()
        colors = []
        for record, level, sender in ((rc, 'INFO', 'RC'), (policy, 'WARN', 'policy:hold'), (mcp, 'ERRO', 'MCP')):
            text = record.formatted.rows[0].text
            assert '1970-01-01T08:00:00.000' in text.plain and '+08:00' not in text.plain
            level_style = text.get_style_at_offset(app.console, text.plain.index('[' + level + ']') + 1)
            sender_style = text.get_style_at_offset(app.console, text.plain.index('[' + sender + ']') + 1)
            assert level_style.color and sender_style.color and level_style != sender_style
            colors.append(level_style.color)
        assert len(set(colors)) == 3


@pytest.mark.asyncio
async def test_colored_metadata_keeps_terminal_control_bytes_literal():
    app = RobotControlApp(FakeController())
    async with app.run_test(size=(160, 30)) as pilot:
        log = app.query_one(StructuredLog)
        log.write_event({'time': 'old\x1b[31m', 'level': 'info\x1b[2J', 'message': 'unchanged'})
        await settled(log, pilot)
        record = next(iter(log.records.values()))
        assert '\x1b' not in record.formatted.document
        assert record.formatted.copy_text == 'unchanged'
