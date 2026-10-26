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

    def status(self):
        return {"state": "paused", "policy": "hold", "queue_remaining": 5,
                "in_flight": False, "generation": 2, "consumed": 3,
                "config_version": 1, "pending_config": None, "exit_code": 0}


@pytest.mark.asyncio
async def test_submission_preserves_json_quotes_history_and_draft():
    controller = FakeController()
    app = RobotControlApp(controller)
    async with app.run_test() as pilot:
        command_input = app.query_one(CommandInput)
        command = 'set policy.params.label "a b [bold]"'
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
        command_input.value = "pol"
        await pilot.press("tab")
        assert command_input.value == "policies "
        command_input.value = 'set policy.params.label "hello world"'
        await pilot.press("tab")
        assert command_input.value == 'set policy.params.label "hello world"'
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
        command_input.value = "quit"
        await pilot.press("enter")
        await pilot.pause(0.08)
        assert controller.commands == ["quit"]
        assert app.is_running
        assert controller.events.empty()
        assert app.query_one(StructuredLog).records
        assert "hold" in str(app.query_one("#status", Static).content)
        controller.done.set()
        await pilot.pause(0.08)


@pytest.mark.asyncio
async def test_policy_commands_refresh_completion_and_submission_errors():
    controller = FakeController()
    available = ["test-command"]
    controller.local_commands = lambda: list(available)
    app = RobotControlApp(controller)
    async with app.run_test() as pilot:
        command_input = app.query_one(CommandInput)
        command_input.value = "test-c"
        await pilot.press("tab")
        assert command_input.value == "test-command "
        available[:] = ["new-command"]
        app.refresh_controller()
        command_input.value = "test-c"
        await pilot.press("tab")
        assert command_input.value == "test-c"
        command_input.value = "new-c"
        await pilot.press("tab")
        assert command_input.value == "new-command "
        def unavailable(line): raise RuntimeError("command queue is full")
        controller.submit = unavailable
        command_input.value = "new-command"
        await pilot.press("enter")
        assert app.is_running
        assert app.query_one(StructuredLog).records
        controller.done.set()
        await pilot.pause(.08)
