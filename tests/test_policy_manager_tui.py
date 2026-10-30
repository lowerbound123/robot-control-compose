import asyncio
import copy
import time

import pytest
from textual.widgets import OptionList
from robot_control.log_tui import StructuredLog

from robot_control.terminal_commands import TerminalCommandRouter
from robot_control.tui import CommandInput, RobotControlApp
from test_controller import until
from test_tui import FakeController


async def eventually(pilot, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'UI condition timed out'
        await pilot.pause(.02)


class EditorController(FakeController):
    def __init__(self):
        super().__init__()
        self.receipts = {}
        self.actions = []
        self.reject_apply = False
        self.saved = ['a', 'b', 'a']
        self.applied = list(self.saved)
        self.token = 0

    def manage_policy(self, action, policies=None, *, token=None):
        self.actions.append((action, policies))
        if action == 'close': return None
        identity = str(len(self.receipts))
        if action == 'open':
            self.token += 1
            result = {'token':self.token, 'policies':self.applied, 'applied':self.applied, 'saved':self.saved,
                      'catalog':[{'id':name, 'valid':True,  'control_mode':'qpos'} for name in ('a','b','c')]}
        elif action == 'save':
            self.saved = list(policies)
            result = {'saved':self.saved}
        else:
            if self.reject_apply:
                self.receipts[identity] = {'status':'failed', 'error':'load failed'}
                return identity
            self.applied = list(policies)
            result = {}
        self.receipts[identity] = {'status':'succeeded', 'result':copy.deepcopy(result)}
        return identity

    def get_operation(self, identity): return self.receipts[identity]


@pytest.mark.asyncio
async def test_editor_numbers_occurrences_but_saves_source_ids():
    controller = EditorController()
    app = RobotControlApp(controller)
    async with app.run_test(size=(100, 30)) as pilot:
        app.router.submit('manage-policy')
        panel = app.query_one('#policy-manager')
        left = app.query_one('#chain-cells', OptionList)
        await eventually(pilot, lambda: panel.display and left.has_focus)
        assert [left.get_option_at_index(i).prompt.plain for i in range(3)] == [
            '01  a-0', '02  b', '03  a-1']
        await pilot.press('ctrl+s')
        assert controller.saved == ['a', 'b', 'a']
        await pilot.press('down', 'down', 'delete')
        assert [left.get_option_at_index(i).prompt.plain for i in range(2)] == ['01  a', '02  b']
        assert panel.policies == ['a', 'b']
        controller.done.set()
        await eventually(pilot, lambda: not app.is_running)


@pytest.mark.asyncio
async def test_editor_disables_candidate_with_colliding_instance_name():
    controller = EditorController()
    app = RobotControlApp(controller)
    async with app.run_test(size=(100, 30)) as pilot:
        app.router.submit('manage-policy')
        panel = app.query_one('#policy-manager')
        left = app.query_one('#chain-cells', OptionList)
        await eventually(pilot, lambda: panel.display and left.has_focus)
        panel.catalog.append({'id': 'a-0', 'valid': True, 'control_mode': 'qpos'})
        await pilot.press('d')
        right = app.query_one('#chain-available', OptionList)
        assert right.get_option_at_index(3).disabled
        assert 'a-0' in right.get_option_at_index(3).prompt.plain
        controller.done.set()
        await eventually(pilot, lambda: not app.is_running)


@pytest.mark.asyncio
async def test_two_columns_insert_above_below_replace_delete_and_cancel():
    controller = EditorController()
    app = RobotControlApp(controller)
    async with app.run_test(size=(100, 30)) as pilot:
        app.router.submit('manage-policy')
        panel = app.query_one('#policy-manager')
        left, right = app.query_one('#chain-cells', OptionList), app.query_one('#chain-available', OptionList)
        await eventually(pilot, lambda: panel.display and left.has_focus)
        assert panel.display and left.has_focus
        assert left.region.x < right.region.x
        assert panel.region.bottom <= app.query_one(CommandInput).region.y
        await pilot.press('d')
        assert right.has_focus
        await pilot.press('down', 'down', 'enter')
        assert panel.policies == ['a','c','b','a'] and left.has_focus
        await pilot.press('u', 'escape')
        assert panel.policies == ['a','c','b','a'] and left.has_focus
        await pilot.press('enter', 'down', 'enter')
        assert panel.policies == ['a','b','b','a']
        await pilot.press('delete')
        assert panel.policies == ['a','b','a']
        await pilot.press('u', 'enter')
        assert panel.policies == ['a','a','b','a'] and left.has_focus
        await pilot.press('delete', 'delete')
        assert panel.policies == ['a','a']
        await pilot.press('escape')
        assert not panel.display and app.query_one(CommandInput).has_focus
        app.router.submit('manage-policy')
        await eventually(pilot, lambda: panel.display and left.has_focus)
        assert panel.policies == ['a','a']
        controller.done.set()
        # Let the app process controller completion before run_test tears down
        # widgets; a running refresh timer must not race forced test teardown.
        await eventually(pilot, lambda: not app.is_running)


@pytest.mark.asyncio
async def test_save_apply_failure_empty_draft_and_shortcuts_do_not_capture_command_input():
    controller = EditorController()
    app = RobotControlApp(controller)
    async with app.run_test(size=(60, 24)) as pilot:
        app.router.submit('manage-policy')
        await pilot.pause(.1)
        panel = app.query_one('#policy-manager')
        await pilot.press('delete')
        await pilot.press('ctrl+s')
        await pilot.pause(.1)
        assert controller.saved == ['b','a'] and controller.applied == ['a','b','a']
        controller.reject_apply = True
        await pilot.press('a')
        await pilot.pause(.1)
        assert controller.actions[-1] == ('apply', ['b', 'a'])
        assert panel.policies == ['b','a'] and panel.display
        assert app.query_one(StructuredLog).records
        await pilot.press('delete', 'delete')
        assert panel.policies == []
        count = len(controller.actions)
        await pilot.press('ctrl+s', 'a', 'shift+enter')
        assert len(controller.actions) == count
        await pilot.press('d', 'enter')
        assert panel.policies == ['a']
        controller.reject_apply = False
        await pilot.press('a')
        await pilot.pause(.1)
        assert controller.applied == ['a']
        await pilot.press('shift+enter')
        await pilot.pause(.1)
        assert controller.actions[-1] == ('apply', ['a'])
        command = app.query_one(CommandInput)
        command.focus()
        await pilot.press('u', 'd', 'a', 'b', 'd', 'd')
        assert command.value == 'udabdd'
        assert panel.policies == ['a']
        await pilot.press('ctrl+c')
        assert controller.commands[-1] == 'exit'
        controller.done.set()


def test_manage_policy_reserved_and_headless_help_and_rejection():
    from rc_policy_sdk.commands import command_specs
    with pytest.raises(ValueError, match='reserved'):
        command_specs({'tui_commands':{'manage-policy':{'handler':'test','args':[]}}})
    controller = EditorController()
    router = TerminalCommandRouter(controller)
    with pytest.raises(ValueError, match='TUI'):
        router.submit('manage-policy')


@pytest.mark.asyncio
async def test_editor_stays_above_command_with_preview_panels_and_small_terminal():
    from robot_control.graphics import GraphicsSupport
    controller = EditorController()
    app = RobotControlApp(controller, graphics=GraphicsSupport('kitty'))
    app._write_graphics = lambda text: None
    async with app.run_test(size=(60, 24)) as pilot:
        app.router.submit('camera on')
        app.router.submit('monitor on')
        app.router.submit('manage-policy')
        await pilot.pause(.1)
        for size in ((60,24), (80,18)):
            await pilot.resize_terminal(*size)
            await pilot.pause(.1)
            panel = app.query_one('#policy-manager')
            command = app.query_one(CommandInput)
            assert 0 <= panel.region.y < panel.region.bottom <= command.region.y
            assert command.region.bottom <= size[1]
            assert app.query_one('#chain-cells').region.bottom <= command.region.y
        # Stop preview timers' work through the user exit path before teardown.
        app.action_safe_exit()
        controller.done.set()
        await asyncio.to_thread(until, lambda: not app.is_running)
