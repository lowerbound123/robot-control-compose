"""Persistent status is a small, literal view; command replies keep full detail."""
import copy
from types import SimpleNamespace

import pytest
from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from robot_control.tui import RobotControlApp
from test_rc_logging import rc, deliver_actions
from test_tui import FakeController


def status_rows(bar):
    table = bar.content
    assert isinstance(table, Table)
    columns = [tuple(column.cells) for column in table.columns]
    return tuple((columns[column][row].plain, columns[column + 1][row].plain)
                 for row in range(table.row_count)
                 for column in range(0, len(table.columns), 2)
                 if columns[column][row].plain)


def assert_full_table_render(bar):
    """Reassemble every physical column from actual Textual output."""
    table = bar.content
    strips = [bar.render_line(y) for y in range(bar.content_size.height)]
    assert all(strip.cell_length <= bar.content_size.width for strip in strips)
    if table.box is None:
        widths = [column.width for column in table.columns]
        lines = [[strip.crop(sum(widths[:index]), sum(widths[:index + 1])).text
                  for index in range(len(widths))] for strip in strips]
    else:
        lines = [strip.text.split('│')[1:-1] for strip in strips if '│' in strip.text]
    assert lines and all(len(line) == len(table.columns) for line in lines)
    for index, column in enumerate(table.columns):
        actual = ''.join(line[index] for line in lines)
        expected = ''.join(cell.plain for cell in column.cells)
        assert ''.join(actual.split()) == ''.join(expected.split())


@pytest.mark.asyncio
async def test_statusbar_keeps_only_the_fixed_fields_in_operator_order():
    controller = FakeController()
    snapshot = {
        'state': 'paused', 'policy': 'example', 'control_owner': 'mcp',
        'in_flight': False, 'consumed': 0,
        'last_action_request': {'request_id': 7, 'trim': 0, 'cached': 4},
        'runtime': 'INITIALIZING', 'target': 'example', 'runtime_error': 'first\nsecond',
        'recording_state': 'failed', 'recording_auto': True, 'recording_error': 'disk\r\nfull',
        'connector_source': 'mock', 'connector_config': 'devices/[bold]相机.yaml',
        'queue_remaining': 999, 'direct': {'phase': 'secret'}, 'future_field': 'hidden',
    }
    untouched = copy.deepcopy(snapshot)
    controller._statusbar_snapshot = lambda: snapshot
    app = RobotControlApp(controller)
    async with app.run_test(size=(160, 30)) as pilot:
        await pilot.pause(.08)
        bar = app.query_one('#status', Static)
        assert status_rows(bar) == (
            ('state', 'paused'), ('policy', 'example'), ('control_owner', 'mcp'),
            ('in_flight', 'False'), ('consumed', '0'), ('last_action_request', '请求 7：裁剪 0，缓存 4'),
            ('runtime', 'INITIALIZING'), ('target', 'example'), ('runtime_error', 'first second'),
            ('recording_state', 'failed'), ('recording_auto', 'on'), ('recording_error', 'disk full'),
            ('connector_source', 'mock'), ('connector_config', 'devices/[bold]相机.yaml'),
        )
        for column in bar.content.columns:
            assert all(isinstance(cell, Text) and not cell.spans for cell in column.cells)
        assert_full_table_render(bar)
        assert snapshot == untouched


@pytest.mark.asyncio
@pytest.mark.parametrize('snapshot', [{}, {
    'state': None, 'policy': '', 'control_owner': None, 'in_flight': None, 'consumed': '',
    'last_action_request': None, 'runtime': '', 'target': None, 'runtime_error': '',
    'recording_state': None, 'recording_error': '', 'connector_source': '', 'connector_config': None,
}])
async def test_statusbar_reserves_every_chunk_when_information_is_absent(snapshot):
    controller = FakeController()
    controller._statusbar_snapshot = lambda: snapshot
    app = RobotControlApp(controller)
    async with app.run_test() as pilot:
        await pilot.pause(.08)
        assert status_rows(app.query_one('#status', Static)) == (
            ('state', '—'), ('policy', '—'), ('control_owner', '—'), ('in_flight', '—'), ('consumed', '—'),
            ('last_action_request', '请求 —：裁剪 —，缓存 —'), ('runtime', '—'), ('target', '—'),
            ('runtime_error', '—'), ('recording_state', '—'), ('recording_auto', '—'), ('recording_error', '—'),
            ('connector_source', '—'), ('connector_config', '—'),
        )


@pytest.mark.parametrize('error', ['\n', '\r\n', '\r', ' \n\t\r\n'])
def test_normalized_empty_errors_keep_their_placeholder(error):
    from robot_control.statusbar import statusbar_rows

    rows = dict(statusbar_rows({'runtime_error': error, 'recording_error': error}))
    assert rows['runtime_error'] == '—'
    assert rows['recording_error'] == '—'


@pytest.mark.asyncio
async def test_statusbar_wraps_full_paths_and_literal_errors_on_real_textual_resize():
    controller = FakeController()
    path = 'devices/' + '路径[bold]_with_spaces /' * 8 + 'camera.yaml'
    error = '执行失败 [red] ' + '完整错误details_' * 10 + '\n最后原因'
    controller._statusbar_snapshot = lambda: {
        'state': 'paused', 'policy': 'example', 'control_owner': 'policy',
        'in_flight': False, 'consumed': 0, 'runtime_error': error,
        'connector_source': 'mock', 'connector_config': path,
    }
    app = RobotControlApp(controller)
    async with app.run_test(size=(120, 50)) as pilot:
        bar = app.query_one('#status', Static)
        heights = []
        for width, groups in ((120, 3), (80, 2), (50, 1), (28, 1), (120, 3)):
            await pilot.resize_terminal(width, 50)
            await pilot.pause(.12)
            values = dict(status_rows(bar))
            assert values['connector_config'] == path
            assert values['runtime_error'] == error.replace('\n', ' ')
            assert len(bar.content.columns) == 2 * groups
            assert not bar.content.show_header
            assert_full_table_render(bar)
            assert app.query_one('#status-area').region.bottom <= app.query_one('#command').region.y
            assert app.query_one('#output').size.height >= 2
            heights.append(bar.size.height)
        assert heights[3] > heights[2] > 1
        assert heights[-1] == heights[0]


@pytest.mark.asyncio
async def test_tiny_terminal_scrolls_status_without_losing_candidates_or_command():
    from robot_control.completion_panel import CompletionPanel
    from robot_control.tui import CommandInput
    from test_tui_input_assistance import settle

    controller = FakeController()
    controller.global_keys = [f'item_{index:02}' for index in range(60)]
    controller._statusbar_snapshot = lambda: {
        'state': 'paused', 'policy': 'example', 'control_owner': 'policy',
        'in_flight': False, 'consumed': 0, 'connector_source': 'mock',
        'runtime_error': '完整错误 ' * 8,
        'connector_config': 'devices/' + 'long-path/' * 10 + 'camera.yaml',
    }
    app = RobotControlApp(controller)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(.08)
        bar = app.query_one('#status', Static)
        viewport = app.query_one('#status-area')
        original = status_rows(bar)
        assert viewport.max_scroll_y > 0  # Long content is bounded even on a wide, short screen.
        field = app.query_one(CommandInput)
        field.value = 'global show '
        field.cursor_position = len(field.value)
        await pilot.press('tab')
        await settle(pilot, lambda: field.completion_result is not None and len(field.completion_result.items) == 60)
        await pilot.resize_terminal(28, 16)
        await pilot.pause(.08)
        panel = app.query_one(CompletionPanel)
        assert field.region.bottom <= app.size.height
        assert panel.region.bottom <= field.region.y
        assert viewport.region.bottom <= panel.region.y
        assert app.query_one('#log-area').size.height >= 2
        assert viewport.max_scroll_y > 0 and status_rows(bar) == original
        assert_full_table_render(bar)
        viewport.scroll_end(animate=False, immediate=True)
        await pilot.pause(.08)
        top = viewport.scroll_offset.y
        rendered = ''.join(bar.render_line(y).text for y in range(top, top + viewport.content_size.height))
        assert 'camera.yaml' in ''.join(rendered.replace('│', '').split())
        assert app.focused is field and controller.commands == []
        await pilot.press('escape')
        await pilot.pause(.08)
        assert not field.menu_open and field.region.bottom <= app.size.height
        assert status_rows(bar) == original
        await pilot.resize_terminal(120, 80)
        await pilot.pause(.08)
        assert viewport.max_scroll_y == 0 and viewport.scroll_y == 0
        assert_full_table_render(bar)
        assert field.region.bottom <= app.size.height and controller.commands == []


@pytest.mark.asyncio
async def test_extremely_narrow_status_keeps_literal_cells_without_border_overflow():
    from robot_control.tui import CommandInput

    controller = FakeController()
    controller._statusbar_snapshot = lambda: {'runtime_error': '中文[red]完整错误', 'connector_config': 'dir/相机.yaml'}
    app = RobotControlApp(controller)
    async with app.run_test(size=(8, 40)) as pilot:
        await pilot.pause(.12)
        bar = app.query_one('#status', Static)
        assert len(bar.content.columns) == 2 and bar.content.box is None
        assert_full_table_render(bar)
        assert dict(status_rows(bar))['connector_config'] == 'dir/相机.yaml'
        assert app.query_one('#output').content_size.height >= 2
        await pilot.press('a', 'b', 'tab', 'escape', 'c')
        field = app.query_one(CommandInput)
        assert field.value == 'abc' and app.focused is field
        assert field.region.bottom <= app.size.height and controller.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['—', 'devices/' + 'long-path/' * 10 + 'camera.yaml'])
async def test_fifty_column_status_keeps_log_space_and_last_field_reachable(path):
    controller = FakeController()
    snapshot = controller._statusbar_snapshot()
    snapshot['connector_config'] = path
    controller._statusbar_snapshot = lambda: snapshot
    app = RobotControlApp(controller)
    async with app.run_test(size=(50, 24)) as pilot:
        await pilot.pause(.12)
        bar = app.query_one('#status', Static)
        viewport = app.query_one('#status-area')
        assert len(status_rows(bar)) == 14 and viewport.size.height <= 8
        assert app.query_one('#output').content_size.height >= 8
        assert viewport.max_scroll_y > 0
        viewport.scroll_end(animate=False, immediate=True)
        await pilot.pause(.08)
        top = viewport.scroll_offset.y
        rendered = ''.join(bar.render_line(y).text for y in range(top, top + viewport.content_size.height))
        assert ('connector_config' if path == '—' else 'camera.yaml') in rendered
        assert_full_table_render(bar)


@pytest.mark.asyncio
async def test_status_click_keeps_command_focus_and_mouse_wheel_still_scrolls():
    from textual.events import MouseScrollDown
    from robot_control.tui import CommandInput
    from test_tui_input_assistance import settle

    controller = FakeController()
    controller._statusbar_snapshot = lambda: {'runtime_error': '完整错误 details ' * 60}
    app = RobotControlApp(controller)
    async with app.run_test(size=(50, 24)) as pilot:
        bar = app.query_one('#status', Static)
        viewport = app.query_one('#status-area')
        field = app.query_one(CommandInput)
        await settle(pilot, lambda: viewport.max_scroll_y > 0)
        await pilot.click(bar, offset=(2, 1))
        await pilot.press('a', 'b')
        assert field.value == 'ab' and app.focused is field
        assert viewport not in app.screen.focus_chain
        # Pilot has no wheel helper. Send the documented wheel event through
        # the real text widget so normal bubbling reaches its scroll container.
        bar.post_message(MouseScrollDown(bar, x=2, y=1, delta_x=0, delta_y=1,
            button=0, shift=False, meta=False, ctrl=False,
            screen_x=bar.region.x + 2, screen_y=viewport.region.y + 1))
        await settle(pilot, lambda: viewport.scroll_y > 0)
        assert field.value == 'ab' and app.focused is field
        field.value = 'policy li'
        field.cursor_position = len(field.value)
        await pilot.press('tab')
        await settle(pilot, lambda: field.completion_result is not None and bool(field.completion_result.items))
        assert field.menu_open and app.focused is field
        await pilot.press('enter', 'escape', 'x')
        assert field.value == 'policy list x' and app.focused is field
        assert controller.commands == []


@pytest.mark.asyncio
async def test_status_table_rebuilds_only_for_visible_values_or_available_width(monkeypatch):
    controller = FakeController()
    snapshot = {'state': 'paused', 'last_action_request': {'request_id': 7, 'trim': 0, 'cached': 4}}
    controller._statusbar_snapshot = lambda: copy.deepcopy(snapshot)
    app = RobotControlApp(controller)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(.08)
        bar = app.query_one('#status', Static)
        initial = bar.content
        updates = []
        original_update = bar.update

        def record_update(content, **kwargs):
            updates.append(content)
            return original_update(content, **kwargs)

        monkeypatch.setattr(bar, 'update', record_update)
        snapshot['hidden_future_value'] = 'ignored'
        for _ in range(3):
            app.refresh_controller()
        await pilot.pause(.16)
        assert bar.content is initial and updates == []
        snapshot['last_action_request']['cached'] = 0
        app.refresh_controller()
        await pilot.pause(.08)
        assert len(updates) == 1
        assert dict(status_rows(bar))['last_action_request'] == '请求 7：裁剪 0，缓存 0'
        snapshot['last_action_request'] = None
        app.refresh_controller()
        await pilot.pause(.08)
        assert len(updates) == 2
        assert dict(status_rows(bar))['last_action_request'] == '请求 —：裁剪 —，缓存 —'
        await pilot.resize_terminal(50, 40)
        await pilot.pause(.12)
        assert len(updates) > 2 and len(bar.content.columns) == 2
        assert_full_table_render(bar)
        stable_count = len(updates)
        await pilot.pause(.16)
        assert len(updates) == stable_count


@pytest.mark.asyncio
async def test_long_status_reserves_log_and_command_space_with_monitor_and_camera():
    from robot_control.graphics import GraphicsSupport
    from robot_control.tui import CommandInput
    from robot_control.tui_panels import CameraPanel, MonitorPanel

    controller = FakeController()
    controller._statusbar_snapshot = lambda: {'runtime_error': '完整错误 details ' * 40}
    controller.monitor_snapshot = lambda: {'qpos': None, 'epos': None}
    controller.camera_preview_snapshot = lambda: {}
    app = RobotControlApp(controller, graphics=GraphicsSupport('kitty'))
    async with app.run_test(size=(120, 40)) as pilot:
        app._set_panel('monitor', True)
        app._set_panel('camera', True)
        camera = app.query_one(CameraPanel)
        monitor = app.query_one(MonitorPanel)
        command = app.query_one(CommandInput)
        for width, height in ((120, 40), (80, 30), (50, 24)):
            await pilot.resize_terminal(width, height)
            await pilot.pause(.15)
            assert app.query_one('#output').content_size.height >= 2
            assert app.query_one('#log-area').region.bottom <= monitor.region.y
            assert monitor.region.bottom <= camera.region.y
            assert camera.region.bottom <= app.query_one('#status-area').region.y
            assert app.query_one('#status-area').region.bottom <= command.region.y
            assert command.region.bottom <= height and app.focused is command
            camera._requested_height = 100
            camera.constrain_height()
            await pilot.pause(.08)
            assert app.query_one('#output').content_size.height >= 2
            assert camera.region.bottom <= app.query_one('#status-area').region.y
        assert controller.commands == []


def test_controller_snapshot_is_local_and_detached_from_public_status(rc, monkeypatch):
    deliver_actions(rc, 23, 4)
    rc.runtime = SimpleNamespace(snapshot=lambda: {
        'phase': 'FAILED', 'target': 'example', 'error': 'runtime\nerror', 'cleanup_forced': True,
    })
    with rc._recorder._lock:
        rc._recorder._state, rc._recorder._error = 'failed', 'recording\nerror'
    before = rc.status()

    def unexpected(*args, **kwargs):
        raise AssertionError('statusbar must not query public status, files, or robot I/O')

    with monkeypatch.context() as local:
        local.setattr(rc, 'status', unexpected)
        local.setattr(rc.registry, 'get', unexpected)
        local.setattr(rc.connector_profiles, 'read', unexpected)
        local.setattr(rc.robot.raw, 'read_state', unexpected)
        local.setattr(rc.robot.raw, 'read_images', unexpected)
        snapshot = rc._statusbar_snapshot()
    assert snapshot['runtime'] == 'FAILED' and snapshot['target'] == 'example'
    assert snapshot['runtime_error'] == 'runtime\nerror'
    assert snapshot['recording_state'] == 'failed' and snapshot['recording_error'] == 'recording\nerror'
    assert snapshot['recording_auto'] is False
    assert snapshot['last_action_request'] == {'request_id': 23, 'generation': 0, 'trim': 0, 'cached': 4}
    snapshot['last_action_request']['cached'] = 100
    snapshot['state'] = 'mutated'
    assert rc.status() == before


@pytest.mark.asyncio
async def test_actual_status_queries_keep_all_hidden_details(rc):
    deliver_actions(rc, 29, 4)
    rc._pending_config = {'params': {'message': 'pending'}}
    rc._hold_name = 'home'
    rc._connector_members = {'example': {'status': 'failed', 'error': 'adaptation failed'}}
    rc._connector_error = 'connection detail'
    rc._connector_target = 'pending-profile'
    with rc._recorder._lock:
        rc._recorder._session = 'session-29'
        rc._recorder._path = '/outputs/recordings/full-path'
        rc._recorder._stats['samples_written'] = 42
    originals = {'status': rc.status(), 'connector status': rc.connector_status(),
                 'record status': rc._record_status()}
    app = RobotControlApp(rc, start_controller=False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause(.08)
        fields = dict(status_rows(app.query_one('#status', Static)))
        for hidden in ('queue_remaining', 'generation', 'config_version', 'pending_config',
                       'hold_name', 'returning', 'direct', 'run_id', 'session_id', 'samples_written'):
            assert hidden not in fields
        # Command replies themselves enqueue recorder log data. Read the full
        # reference immediately before each query, including those live counts.
        for line, read in (('status', rc.status), ('connector status', rc.connector_status),
                           ('record status', rc._record_status)):
            expected = read()
            receipt = rc.get_operation(app.router.submit(line))
            assert receipt['status'] == 'succeeded'
            assert receipt['result'] == expected
        assert originals['status']['queue_remaining'] == 4
        assert originals['status']['last_action_request']['request_id'] == 29
        assert originals['status']['pending_config'] == {'params': {'message': 'pending'}}
        assert originals['connector status']['instances']['example']['error'] == 'adaptation failed'
        assert originals['connector status']['target_profile'] == 'pending-profile'
        assert originals['record status']['samples_written'] == 42
        assert originals['record status']['path'] == '/outputs/recordings/full-path'
