"""Real Textual key dispatch: completion confirmation never submits commands."""
import asyncio

import pytest
from textual import events
from textual.widgets import Input

from robot_control.tui import CommandInput, RobotControlApp
from test_tui import FakeController
from test_controller import setup_controller


async def settle(pilot, predicate, timeout=3):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, 'input assistance condition timed out'
        await pilot.pause(.03)


@pytest.mark.asyncio
async def test_tab_opens_unique_menu_and_enter_confirms_without_submitting():
    controller = FakeController()
    app = RobotControlApp(controller)
    async with app.run_test() as pilot:
        field = app.query_one(CommandInput)
        field.value = 'policy li'
        field.cursor_position = len(field.value)
        await pilot.press('tab')
        assert field.value == 'policy li'
        assert field.menu_open
        assert app.focused is field
        await pilot.press('enter')
        assert field.value == 'policy list ' and not field.menu_open
        assert controller.commands == [] and field.history == []
        await pilot.press('enter')
        assert controller.commands == ['policy list ']
        controller.done.set(); await pilot.pause(.08)


@pytest.mark.asyncio
async def test_history_ghost_right_accepts_but_enter_and_escape_never_accept():
    controller = FakeController()
    app = RobotControlApp(controller)
    async with app.run_test() as pilot:
        field = app.query_one(CommandInput)
        field.remember('policy help example')
        field.remember('policy help example-1')
        field.value = 'policy help'
        field.cursor_position = len(field.value)
        await settle(pilot, lambda: field._suggestion == 'policy help example-1')
        await pilot.press('right')
        assert field.value == 'policy help example-1' and controller.commands == []
        field.value = 'policy help'; field.cursor_position = len(field.value)
        await settle(pilot, lambda: bool(field._suggestion))
        await pilot.press('enter')
        assert controller.commands == ['policy help']
        field.value = 'policy help'; field.cursor_position = len(field.value)
        await settle(pilot, lambda: bool(field._suggestion))
        await pilot.press('escape')
        assert not field._suggestion
        await pilot.pause(.1)
        assert not field._suggestion
        await pilot.press('enter')
        assert controller.commands == ['policy help', 'policy help']
        controller.done.set(); await pilot.pause(.08)


@pytest.mark.asyncio
async def test_menu_editing_empty_confirmation_and_middle_cursor_do_not_submit():
    controller = FakeController()
    app = RobotControlApp(controller)
    async with app.run_test() as pilot:
        field = app.query_one(CommandInput)
        field.value = 'ca'; field.cursor_position = 2
        await pilot.press('tab', 'm')
        assert field.menu_open and field.value == 'cam'
        await pilot.press('space')
        assert field.value == 'camera ' and not field.menu_open
        field.value = 'nothing-matches'; field.cursor_position = len(field.value)
        await pilot.press('tab', 'enter')
        assert field.value == 'nothing-matches' and not field.menu_open
        assert controller.commands == []
        await pilot.press('tab', 'space')
        assert field.value == 'nothing-matches ' and not field.menu_open
        field.value = 'global set note "中文 文字"'
        field.cursor_position = 2
        await pilot.press('tab', 'home')
        assert not field.menu_open and field.cursor_position == 0
        field.post_message(events.Paste('粘贴\n文本'))
        await pilot.pause(.03)
        assert field.value.startswith('粘贴 文本global')
        assert controller.commands == []
        controller.done.set(); await pilot.pause(.08)


@pytest.mark.asyncio
async def test_history_persists_real_submissions_including_business_failure_and_excludes_secrets(tmp_path):
    controller = FakeController()
    controller.config_path, controller.outputs = tmp_path / 'rc.yaml', tmp_path / 'outputs'
    raw = '  global set note "原样  空格"  '
    app = RobotControlApp(controller)
    async with app.run_test() as pilot:
        field = app.query_one(CommandInput)
        field.value = raw; field.cursor_position = len(raw)
        await pilot.press('enter')
        def business_failure(line):
            controller.commands.append(line)
            raise RuntimeError('policy unavailable')
        controller.submit = business_failure
        field.value = 'policy absent inspect'; field.cursor_position = len(field.value)
        await pilot.press('enter')
        for invalid in ('made-up-command', 'global set api_key "SECRET-CANARY"'):
            field.value = invalid; field.cursor_position = len(field.value)
            await pilot.press('enter')
        assert field.history == [raw, 'policy absent inspect']
        assert controller.commands == [raw, 'policy absent inspect']
        assert await asyncio.to_thread(app.command_history.wait_idle, 3)
        saved_path = app.command_history.path
        controller.done.set(); await pilot.pause(.08)
    assert 'SECRET-CANARY' not in saved_path.read_text()
    second = FakeController()
    second.config_path, second.outputs = controller.config_path, controller.outputs
    restored = RobotControlApp(second)
    async with restored.run_test() as pilot:
        field = restored.query_one(CommandInput)
        await settle(pilot, lambda: len(field.history) == 2)
        field.value = 'draft 中文'; field.cursor_position = len(field.value)
        await pilot.press('up', 'up')
        assert field.value == raw
        await pilot.press('down', 'down')
        assert field.value == 'draft 中文'
        assert second.commands == []
        second.done.set(); await pilot.pause(.08)


@pytest.mark.asyncio
async def test_ghost_is_suppressed_for_selection_middle_cursor_and_stale_history_reply():
    from robot_control.tui import HistorySuggestionReady
    from textual.widgets._input import Selection
    controller = FakeController()
    app = RobotControlApp(controller)
    async with app.run_test() as pilot:
        field = app.query_one(CommandInput)
        field.remember('global show 中文')
        field.value = 'global show'; field.cursor_position = len(field.value)
        await settle(pilot, lambda: bool(field._suggestion))
        field.cursor_position = 2
        await pilot.pause(.03)
        assert not field._suggestion
        await pilot.press('right')
        assert field.value == 'global show' and field.cursor_position == 3
        field.selection = Selection(0, len(field.value))
        await pilot.pause(.03)
        assert not field._suggestion
        field.cursor_position = len(field.value)
        await settle(pilot, lambda: bool(field._suggestion))
        await pilot.press('escape')
        field.post_message(HistorySuggestionReady(field.value, 'global show STALE', field.history_revision, field._ghost_epoch - 1))
        await pilot.pause(.03)
        assert not field._suggestion and controller.commands == []
        field.value = ''
        await pilot.pause(.03)
        assert not field._suggestion
        controller.done.set(); await pilot.pause(.08)


@pytest.mark.asyncio
async def test_candidate_grid_navigation_wraps_scrolls_and_keeps_input_visible():
    from robot_control.completion_panel import CompletionPanel
    controller = FakeController()
    controller.global_keys = [f'item_{index:02}' for index in range(60)]
    app = RobotControlApp(controller)
    async with app.run_test(size=(70, 28)) as pilot:
        field = app.query_one(CommandInput)
        field.value = 'global show '; field.cursor_position = len(field.value)
        await pilot.press('tab')
        await settle(pilot, lambda: field.completion_result is not None and len(field.completion_result.items) == 60)
        panel = app.query_one(CompletionPanel)
        assert panel.columns > 1 and panel.visible_rows <= 6
        await pilot.press('down')
        assert field.selected_completion == panel.columns
        await pilot.press('right', 'up')
        assert field.selected_completion == 1
        await pilot.press('left', 'shift+tab')
        assert field.selected_completion == 59 and panel.top_row > 0
        await pilot.press('tab')
        assert field.selected_completion == 0
        await pilot.resize_terminal(28, 16)
        await pilot.pause(.05)
        assert field.region.bottom <= app.size.height
        assert panel.region.bottom <= field.region.y
        assert app.focused is field and controller.commands == []
        await pilot.resize_terminal(16, 40)
        await pilot.pause(.05)
        assert panel.columns == 1
        await pilot.press('right')
        assert field.selected_completion == 1
        await pilot.press('left', 'up')
        assert field.selected_completion == 59
        await pilot.press('down')
        assert field.selected_completion == 0
        await pilot.press('escape')
        assert field.value == 'global show '
        controller.done.set(); await pilot.pause(.08)


@pytest.mark.asyncio
async def test_candidate_metadata_control_bytes_are_literal_and_insertion_stays_raw():
    from robot_control.completion import CompletionItem, CompletionResult
    from robot_control.completion_panel import CompletionPanel
    from textual.widgets import Static
    controller = FakeController()
    app = RobotControlApp(controller)
    async with app.run_test() as pilot:
        field = app.query_one(CommandInput)
        field.value = 'global show '; field.cursor_position = len(field.value)
        await pilot.press('tab')
        result = CompletionResult([CompletionItem('中文\x1b[31m\x7f', '正常 空格', '说明\x00\x1b[2J', 'str')],
                                  len(field.value), len(field.value), text=field.value, cursor=len(field.value), revision=100000)
        field._accept_result(result)
        panel = app.query_one(CompletionPanel)
        label = panel.query_one('#completion-grid', Static).content.plain
        help_text = panel.query_one('#completion-detail', Static).content.plain
        assert '\x1b' not in label + help_text and '\x00' not in help_text and '\x7f' not in label
        assert '\\x1b' in help_text and '\\x00' in help_text
        await pilot.press('enter')
        assert '正常 空格' in field.value and controller.commands == []
        controller.done.set(); await pilot.pause(.08)


@pytest.mark.asyncio
async def test_candidate_and_ghost_acceptance_do_not_interrupt_real_waiting_batch(setup_controller):
    from test_controller import ready
    controller, _, _ = setup_controller
    ready(controller)
    app = RobotControlApp(controller, start_controller=False)
    async with app.run_test() as pilot:
        field = app.query_one(CommandInput)
        field.value = 'help'; field.cursor_position = 4
        await pilot.press('enter')
        with controller._lock:
            batch = controller.submit_tui_batch('status')
            field.value = 'he'; field.cursor_position = 2
            await settle(pilot, lambda: field._suggestion == 'help')
            await pilot.press('right')
            assert field.value == 'help'
            field.value = 'policy li'; field.cursor_position = len(field.value)
            await pilot.press('tab', 'enter', 'escape')
            assert field.value == 'policy list '
            assert controller.get_operation(batch)['status'] == 'queued'
            assert field.history == ['help']
        result = await asyncio.to_thread(controller.wait_operation, batch, 3)
        assert result['status'] == 'succeeded'
        controller.submit('exit')
        await settle(pilot, controller.done.is_set)
        await pilot.pause(.08)


@pytest.mark.asyncio
async def test_directory_confirmation_keeps_separator_and_tab_lists_children(tmp_path):
    import shlex
    controller = FakeController()
    controller.pose_root = tmp_path / 'poses'
    directory = controller.pose_root / '中文 空格'
    directory.mkdir(parents=True)
    target = directory / 'home.json'
    target.write_text('{}')
    app = RobotControlApp(controller)
    async with app.run_test() as pilot:
        field = app.query_one(CommandInput)
        field.value = 'pose restore '; field.cursor_position = len(field.value)
        await pilot.press('tab')
        await settle(pilot, lambda: field.completion_result is not None and any(
            item.kind == 'directory' for item in field.completion_result.items))
        await pilot.press('enter')
        assert field.value.rstrip("'\"").endswith('/') and not field.value.endswith(' ')
        assert not field.menu_open and controller.commands == []
        await pilot.press('tab')
        await settle(pilot, lambda: field.completion_result is not None and any(
            item.label == 'home.json' for item in field.completion_result.items))
        await pilot.press('space')
        assert shlex.split(field.value) == ['pose', 'restore', str(target)]
        assert field.value.endswith(' ') and not field.menu_open
        assert controller.commands == []
        controller.done.set(); await pilot.pause(.08)


@pytest.mark.asyncio
async def test_open_menu_drops_removed_instance_before_confirmation():
    controller = FakeController()
    paths = ['policy example-1 inspect']
    controller.local_commands = lambda: list(paths)
    app = RobotControlApp(controller)
    async with app.run_test() as pilot:
        field = app.query_one(CommandInput)
        field.value = 'policy example-1 ins'; field.cursor_position = len(field.value)
        await pilot.press('tab')
        await settle(pilot, lambda: field.completion_result is not None and bool(field.completion_result.items))
        paths[:] = ['policy example-0 inspect']
        await settle(pilot, lambda: field.completion_result is not None and not field.completion_result.items)
        await pilot.press('enter')
        assert field.value == 'policy example-1 ins' and not field.menu_open
        assert controller.commands == [] and field.history == []
        controller.done.set(); await pilot.pause(.08)


@pytest.mark.asyncio
async def test_right_does_not_accept_ghost_from_an_obsolete_history_revision(tmp_path):
    controller = FakeController()
    controller.config_path, controller.outputs = tmp_path / 'rc.yaml', tmp_path / 'outputs'
    app = RobotControlApp(controller)
    async with app.run_test() as pilot:
        field = app.query_one(CommandInput)
        field.remember('global show earlier')
        await settle(pilot, lambda: 'global show earlier' in field.history)
        field.value = 'global show '; field.cursor_position = len(field.value)
        await settle(pilot, lambda: field._suggestion == 'global show earlier')
        app.command_history.record('global show latest')
        # Same event-loop turn: the 50 ms UI poll has not observed the revision.
        field.action_cursor_right()
        assert field.value == 'global show '
        await settle(pilot, lambda: field._suggestion == 'global show latest')
        await pilot.press('right')
        assert field.value == 'global show latest' and controller.commands == []
        controller.done.set(); await pilot.pause(.08)


@pytest.mark.asyncio
@pytest.mark.parametrize('line', ['made-up-command', 'switch example', 'global set api_key "DO-NOT-ECHO"'])
async def test_invalid_real_submission_interrupts_waiting_batch_without_entering_history(setup_controller, line):
    from test_controller import ready
    controller, _, _ = setup_controller
    ready(controller)
    app = RobotControlApp(controller, start_controller=False)
    async with app.run_test() as pilot:
        field = app.query_one(CommandInput)
        with controller._lock:
            batch = controller.submit_tui_batch('status')
            field.value = line; field.cursor_position = len(line)
            await pilot.press('enter')
            assert controller.get_operation(batch)['status'] == 'cancelled'
            assert field.history == []
            from robot_control.log_tui import StructuredLog
            assert 'DO-NOT-ECHO' not in str(app.query_one(StructuredLog).records)
        controller.submit('exit')
        await settle(pilot, controller.done.is_set)
        await pilot.pause(.08)


@pytest.mark.parametrize('pasted', [False, True])
def test_real_terminal_burst_edit_and_tab_preserve_input_order(tmp_path, pasted):
    import fcntl
    import json
    import os
    from pathlib import Path
    import pty
    import select
    import struct
    import subprocess
    import sys
    import termios
    import time
    import tty
    root = Path(__file__).resolve().parents[1]
    master, slave = pty.openpty()
    tty.setraw(slave)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
    state_path = tmp_path / 'state.json'
    environment = {**os.environ, 'TERM': 'xterm-256color', 'PYTHONPATH': os.pathsep.join((str(root / 'src'), str(root / 'packages/policy-sdk/src')))}
    child = subprocess.Popen([sys.executable, str(root / 'tests/fixtures/completion_terminal.py'), str(state_path)],
                             stdin=slave, stdout=slave, stderr=slave, env=environment, start_new_session=True)
    os.close(slave)
    output = bytearray()
    def state():
        return json.loads(state_path.read_text()) if state_path.exists() else {}
    def wait_for(predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if select.select([master], [], [], .02)[0]:
                try: output.extend(os.read(master, 65536))
                except OSError: pass
            if predicate():
                return
            assert child.poll() is None, bytes(output[-2000:]).decode(errors='replace')
        pytest.fail(f'rapid terminal input changed: {state()}')
    try:
        wait_for(lambda: state().get('width') == 80)
        os.write(master, b'policy li')
        wait_for(lambda: state().get('value') == 'policy li')
        typed = b'camera sh'
        if pasted:
            typed = b'\x1b[200~' + typed + b'\x1b[201~'
        os.write(master, b'\x15' + typed + b'\t')
        wait_for(lambda: state().get('menu') and state().get('value') == 'camera sh')
        assert state()['commands'] == []
        assert state()['items'] == ['show']
        os.write(master, b'\x11')
        wait_for(lambda: child.poll() is not None)
        assert child.returncode == 0
    finally:
        if child.poll() is None:
            child.terminate()
            try: child.wait(3)
            except subprocess.TimeoutExpired: child.kill(); child.wait(3)
        os.close(master)


@pytest.mark.asyncio
async def test_enum_selection_detail_shows_real_type_and_help_only_once():
    from robot_control.completion_panel import CompletionPanel
    from textual.widgets import Static
    controller = FakeController()
    description = '选择检查模式；提示不会执行命令'
    controller.completion_snapshot = lambda: {'instances': {'example': {'commands': {}, 'config_hints': {'settings': {
        'params.mode': {'name': '模式', 'type': 'enum', 'choices': ['普通', '仔细'], 'help': description},
    }}}}}
    app = RobotControlApp(controller)
    async with app.run_test() as pilot:
        field = app.query_one(CommandInput)
        field.value = 'policy example config set params.mode '; field.cursor_position = len(field.value)
        await pilot.press('tab')
        await settle(pilot, lambda: field.completion_result is not None and len(field.completion_result.items) == 2)
        text = app.query_one(CompletionPanel).query_one('#completion-detail', Static).content.plain
        assert text.count(description) == 1
        assert '普通 · enum' in text and ' · value' not in text
        assert controller.commands == []
        controller.done.set(); await pilot.pause(.08)
