import asyncio
import queue
import threading

import pytest
from textual.widgets import Static
from robot_control.log_tui import StructuredLog

from robot_control.tui import CommandInput, RobotControlApp


class FakeController:
    def __init__(self):
        self.events = queue.Queue()
        self.done = threading.Event()
        self.exit_code = 0
        self.commands = []
        self.starts = 0

    def start(self):
        self.starts += 1

    def submit(self, line):
        self.commands.append(line)

    def completion_snapshot(self):
        instances = {}
        for path in getattr(self, 'local_commands', lambda: [])():
            words = path.split()
            if len(words) >= 3 and words[0] == 'policy':
                entry = instances.setdefault(words[1], {'commands': {}})
                if len(words) == 3 and words[2] not in ('config', 'retry'):
                    entry['commands'][words[2]] = {'args': [], 'help': words[2]}
        for name in getattr(getattr(self, 'policy_monitors', None), 'available', lambda: [])():
            instances.setdefault(name, {'commands': {}})
        snapshot = {'instances': instances, 'sources': list(instances), 'global_keys': getattr(self, 'global_keys', [])}
        if hasattr(self, 'pose_root'):
            snapshot['pose_root'] = str(self.pose_root)
        return snapshot

    def status(self):
        return {"state": "paused", "policy": "hold", "queue_remaining": 5,
                "in_flight": False, "generation": 2, "consumed": 3,
                "config_version": 1, "pending_config": None, "exit_code": 0}


async def wait_completion(pilot, field, labels):
    deadline = asyncio.get_running_loop().time() + 3
    while True:
        result = field.completion_result
        if result is not None and result.text == field.value and {item.label for item in result.items} == set(labels):
            return
        assert asyncio.get_running_loop().time() < deadline, 'completion catalog did not converge'
        await pilot.pause(.03)


@pytest.mark.asyncio
async def test_submission_preserves_json_quotes_history_and_draft():
    controller = FakeController()
    app = RobotControlApp(controller)
    async with app.run_test() as pilot:
        command_input = app.query_one(CommandInput)
        command = 'policy first config set params.label "a b [bold]"'
        command_input.value = command
        await pilot.press("enter")
        assert controller.commands == [command]
        assert command_input.value == ""
        command_input.value = "unsubmitted draft"
        await pilot.press("up")
        assert command_input.value == command
        await pilot.press("down")
        assert command_input.value == "unsubmitted draft"
        controller.done.set()
        await pilot.pause(0.08)
    assert controller.starts == 1


@pytest.mark.asyncio
async def test_tab_completes_command_without_rewriting_json():
    controller = FakeController()
    app = RobotControlApp(controller)
    async with app.run_test() as pilot:
        command_input = app.query_one(CommandInput)
        command_input.value = "policy li"
        command_input.cursor_position = len(command_input.value)
        await pilot.press("tab")
        assert command_input.value == "policy li" and command_input.menu_open
        await wait_completion(pilot, command_input, ["list"])
        await pilot.press("enter")
        assert command_input.value == "policy list "
        command_input.value = 'policy first config set params.label "hello world"'
        await pilot.press("tab")
        assert command_input.value == 'policy first config set params.label "hello world"'
        await pilot.press('escape')
        controller.done.set()
        await pilot.pause(0.08)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["ctrl+c", "ctrl+q"])
async def test_exit_keys_wait_for_controller_cleanup(key):
    controller = FakeController()
    app = RobotControlApp(controller)
    async with app.run_test() as pilot:
        await pilot.press(key)
        await pilot.pause(0.08)
        assert controller.commands == ["exit"]
        assert app.is_running
        controller.exit_code = 7
        controller.done.set()
        await pilot.pause(0.08)
    assert app.return_code == 7


@pytest.mark.asyncio
async def test_exit_command_and_events_remain_visible_until_done():
    controller = FakeController()
    controller.events.put({"time": "12:01:02", "level": "INFO", "message": "[bold] raw output"})
    app = RobotControlApp(controller)
    async with app.run_test() as pilot:
        command_input = app.query_one(CommandInput)
        command_input.value = "exit"
        await pilot.press("enter")
        await pilot.pause(0.08)
        assert controller.commands == ["exit"]
        assert app.is_running
        assert controller.events.empty()
        assert app.query_one(StructuredLog).records
        assert "hold" in str(app.query_one("#status", Static).content)
        controller.done.set()
        await pilot.pause(0.08)


@pytest.mark.asyncio
async def test_policy_commands_refresh_completion_and_submission_errors():
    controller = FakeController()
    available = ["policy hold test-command"]
    controller.local_commands = lambda: list(available)
    app = RobotControlApp(controller)
    async with app.run_test() as pilot:
        command_input = app.query_one(CommandInput)
        command_input.value = "policy hold test-c"
        command_input.cursor_position = len(command_input.value)
        await pilot.press("tab")
        assert command_input.value == "policy hold test-c"
        await wait_completion(pilot, command_input, ["test-command"])
        await pilot.press("enter")
        assert command_input.value == "policy hold test-command "
        available[:] = ["policy hold new-command"]
        app.refresh_controller()
        command_input.value = "policy hold test-c"
        await pilot.press("tab")
        assert command_input.value == "policy hold test-c"
        await wait_completion(pilot, command_input, [])
        await pilot.press("escape")
        command_input.value = "policy hold new-c"
        command_input.cursor_position = len(command_input.value)
        await pilot.press("tab")
        await wait_completion(pilot, command_input, ["new-command"])
        await pilot.press("enter")
        assert command_input.value == "policy hold new-command "
        def unavailable(line): raise RuntimeError("command queue is full")
        controller.submit = unavailable
        command_input.value = "policy hold new-command"
        await pilot.press("enter")
        assert app.is_running
        assert app.query_one(StructuredLog).records
        controller.done.set()
        await pilot.pause(.08)
