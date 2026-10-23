import asyncio
import copy
import time

import pytest
from textual.widgets import OptionList, RichLog

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
        self.saved = ['A', 'B', 'A']
        self.applied = list(self.saved)
        self.token = 0

    def manage_policy(self, action, policies=None, *, token=None):
        self.actions.append((action, policies))
        if action == 'close': return None
        identity = str(len(self.receipts))
        if action == 'open':
            self.token += 1
            result = {'token':self.token, 'policies':self.applied, 'applied':self.applied, 'saved':self.saved,
                      'catalog':[{'id':name, 'valid':True, 'policy_host':'env-'+name, 'control_mode':'qpos'} for name in ('A','B','C')]}
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
async def test_two_columns_insert_above_below_replace_delete_and_cancel(monkeypatch):
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
        await pilot.press('b')
        assert right.has_focus
        await pilot.press('down', 'down', 'enter')
        assert panel.policies == ['A','C','B','A'] and left.has_focus
        await pilot.press('a', 'escape')
        assert panel.policies == ['A','C','B','A'] and left.has_focus
        await pilot.press('enter', 'down', 'enter')
        assert panel.policies == ['A','B','B','A']
        await pilot.press('delete')
        assert panel.policies == ['A','B','A']
        # Test the intended rapid dd gesture independently of CI render speed.
        from types import SimpleNamespace
        from robot_control import policy_manager_tui
        ticks = iter([100., 100.1])
        monkeypatch.setattr(policy_manager_tui, 'time', SimpleNamespace(monotonic=lambda: next(ticks)))
        await pilot.press('d', 'd')
        assert panel.policies == ['A','A']
        await pilot.press('escape')
        assert not panel.display and app.query_one(CommandInput).has_focus
        app.router.submit('manage-policy')
        await eventually(pilot, lambda: panel.display and left.has_focus)
        assert panel.policies == ['A','A']
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
        assert controller.saved == ['B','A'] and controller.applied == ['A','B','A']
        controller.reject_apply = True
        await pilot.press('shift+enter')
        await pilot.pause(.1)
        assert controller.actions[-1] == ('apply', ['B', 'A'])
        assert panel.policies == ['B','A'] and panel.display
        assert app.query_one(RichLog).lines
        await pilot.press('delete', 'delete')
        assert panel.policies == []
        count = len(controller.actions)
        await pilot.press('ctrl+s', 'shift+enter')
        assert len(controller.actions) == count
        await pilot.press('b', 'enter')
        assert panel.policies == ['A']
        command = app.query_one(CommandInput)
        command.focus()
        await pilot.press('a', 'b', 'd', 'd')
        assert command.value == 'abdd'
        assert panel.policies == ['A']
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
