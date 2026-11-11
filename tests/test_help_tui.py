import threading

import pytest
from rich.cells import cell_len

from robot_control.help_document import document, help_text
from robot_control.log_tui import StructuredLog
from robot_control.tui import RobotControlApp
from test_help_render import sample_help
from test_structured_log_tui import point, settled
from test_tui import FakeController


@pytest.mark.asyncio
async def test_help_event_is_expanded_and_whole_copy_preserves_final_json_example():
    value = document('命令帮助', examples=[{'title': '', 'command': '{"x": 1}'}])
    raw = '命令帮助\n\n示例\n{"x": 1}'
    controller = FakeController()
    controller.events.put({'level': 'INFO', 'message': raw, 'help_document': value})
    app = RobotControlApp(controller)
    async with app.run_test(size=(120, 40)) as pilot:
        log = app.query_one(StructuredLog)
        await settled(log, pilot)
        record = next(iter(log.records.values()))
        assert record.formatted.parsed is None
        assert len(record.formatted.rows) == 4
        await pilot.click(log, offset=point(log, log.scrollable_content_region.width - 3))
        assert app.clipboard == raw
        await pilot.click(log, offset=point(log, 3))
        await settled(log, pilot)
        assert len(record.formatted.rows) == 4


def find_row(record, text):
    return next((index, row) for index, row in enumerate(record.formatted.rows)
                if text in row.text.plain)


@pytest.mark.asyncio
async def test_rendered_cjk_columns_align_in_terminal_cells_and_keep_semantic_styles():
    app = RobotControlApp(FakeController())
    value = sample_help()
    async with app.run_test(size=(120, 60)) as pilot:
        log = app.query_one(StructuredLog)
        log.write_event({'message': help_text(value), 'help_document': value})
        await settled(log, pilot)
        record = next(iter(log.records.values()))
        table_lines = [log.render_line(index) for index, row in enumerate(record.formatted.rows)
                       if '│' in row.text.plain]
        assert len(table_lines) >= 5
        assert {cell_len(line.text.split('│')[0]) for line in table_lines} == {15}
        for line in table_lines:
            assert line.cell_length == log.scrollable_content_region.width
        title_y, _ = find_row(record, '用法')
        assert cell_len(log.render_line(title_y).text.strip()) == 4
        name_y, _ = find_row(record, '目标阈值')
        name_style = next(segment.style for segment in log.render_line(name_y) if '目标阈值' in segment.text)
        desc_y, _ = find_row(record, '速度比例')
        desc_style = next(segment.style for segment in log.render_line(desc_y) if '速度比例' in segment.text)
        assert name_style.color != desc_style.color
        assert name_style.bold and not desc_style.dim


@pytest.mark.asyncio
@pytest.mark.parametrize('reverse', [False, True])
async def test_parameter_drag_copies_source_and_survives_switch_to_vertical_layout(reverse):
    app = RobotControlApp(FakeController())
    value = sample_help()
    async with app.run_test(size=(120, 60)) as pilot:
        log = app.query_one(StructuredLog)
        log.write_event({'message': help_text(value), 'help_document': value})
        await settled(log, pilot)
        record = next(iter(log.records.values()))
        # Locate the actual parameter, not usage/example mentions.
        name_y, name = next((index, row) for index, row in enumerate(record.formatted.rows)
                            if row.text.plain.startswith('gain'))
        desc = '速度比例，修改后在下一次动作生效。'
        desc_y, desc_row = find_row(record, desc)
        assert '│' in name.text.plain
        left = point(log, 2, name_y)
        right = point(log, 2 + desc_row.text.cell_len, desc_y)
        start, end = (right, left) if reverse else (left, right)
        await pilot.mouse_down(log, offset=start)
        await pilot.hover(log, offset=end)
        await pilot.mouse_up(log, offset=end)
        expected = 'gain  number · 可选 · 默认 1\n' + desc
        await pilot.press('ctrl+shift+c')
        assert app.clipboard == expected
        for row_index in (name_y, desc_y):
            characters = [(char, segment.style) for segment in log.render_line(row_index) for char in segment.text]
            border = next(style for char, style in characters if char == '│')
            glyph = next(style for char, style in characters if char in 'gain速')
            assert border.bgcolor != glyph.bgcolor
        await pilot.resize_terminal(50, 60)
        await settled(log, pilot)
        assert log.selected_text == expected
        assert not any('│' in row.text.plain for row in record.formatted.rows)
        await pilot.press('ctrl+shift+c')
        assert app.clipboard == expected
        log.copy_record(record.identity)
        assert app.clipboard == help_text(value)


@pytest.mark.asyncio
async def test_long_chinese_parameter_selection_scrolls_and_keeps_logical_newlines():
    value = document('长参数帮助', parameters=[{
        'name': 'config.' + 'parameter_' * 8, 'type': 'string', 'required': True,
        'description': '中文内容 alpha [bold] ' * 30,
    }])
    app = RobotControlApp(FakeController())
    async with app.run_test(size=(50, 24)) as pilot:
        log = app.query_one(StructuredLog)
        log.write_event({'message': help_text(value), 'help_document': value})
        await settled(log, pilot)
        record = next(iter(log.records.values()))
        log.scroll_home(animate=False, immediate=True)
        await pilot.pause()
        name_y, _ = find_row(record, 'config.')
        await pilot.mouse_down(log, offset=point(log, 2, name_y))
        # Mouse capture must preserve the start while the log scrolls.
        await pilot.hover(log, offset=point(log, 3, name_y + 1))
        log.scroll_end(animate=False, immediate=True, x_axis=False)
        await pilot.pause()
        last = record.formatted.rows[-1]
        end = point(log, 2 + last.text.cell_len, len(record.formatted.rows) - 1 - log.scroll_offset.y)
        await pilot.hover(log, offset=end)
        await pilot.mouse_up(log, offset=end)
        await pilot.press('ctrl+shift+c')
        expected = 'config.' + 'parameter_' * 8 + '  string · 必填\n' + '中文内容 alpha [bold] ' * 30
        assert app.clipboard == expected
        await pilot.resize_terminal(18, 24)
        await settled(log, pilot)
        assert log.selected_text == expected


@pytest.mark.asyncio
async def test_help_resize_formats_off_ui_thread_and_discards_stale_layout(monkeypatch):
    from robot_control import log_tui
    from robot_control.tui import CommandInput

    original = log_tui.format_record
    entered, proceed = threading.Event(), threading.Event()
    worker_threads = []

    def delayed(message, **options):
        if options.get('help_document') is not None and options['width'] < 60:
            worker_threads.append(threading.get_ident())
            entered.set()
            assert proceed.wait(8)
        return original(message, **options)

    monkeypatch.setattr(log_tui, 'format_record', delayed)
    app = RobotControlApp(FakeController())
    value = sample_help()
    try:
        async with app.run_test(size=(120, 60)) as pilot:
            log = app.query_one(StructuredLog)
            log.write_event({'message': help_text(value), 'help_document': value})
            await settled(log, pilot)
            record = next(iter(log.records.values()))
            await pilot.resize_terminal(50, 60)
            for _ in range(100):
                if entered.is_set():
                    break
                await pilot.pause(.01)
            assert entered.is_set()
            app.query_one(CommandInput).focus()
            await pilot.press('x')
            assert app.query_one(CommandInput).value == 'x'
            await pilot.resize_terminal(120, 60)
            proceed.set()
            await settled(log, pilot)
            assert all(thread != threading.get_ident() for thread in worker_threads)
            assert any('│' in row.text.plain for row in record.formatted.rows)
            assert record.formatted.document.endswith(help_text(value))
            log.copy_record(record.identity)
            assert app.clipboard == help_text(value)
    finally:
        proceed.set()
