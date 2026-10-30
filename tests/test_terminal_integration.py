import asyncio
import io
import threading

import pytest
from robot_control.log_tui import StructuredLog

from test_controller import setup_controller as setup_controller, until
from test_mcp_local import local_service as local_service, local_call


@pytest.mark.asyncio
async def test_tui_completes_tools_preserves_arguments_and_logs_errors(local_service):
    from robot_control.terminal_commands import TerminalCommandRouter
    from robot_control.tui import CommandInput, RobotControlApp

    controller, _, service = local_service
    router = TerminalCommandRouter(controller, service)
    app = RobotControlApp(controller, start_controller=False, router=router)
    async with app.run_test() as pilot:
        command = app.query_one(CommandInput)
        for prefix, expected in (("mc", "mcp "), ("mcp se", "mcp send_command "),
                                 ("mcp ret", "mcp return"), ("mcp return_", "mcp return_and_wait "),
                                 ("mcp get_pol", "mcp get_policy_result ")):
            command.value = prefix
            await pilot.press("tab")
            assert command.value == expected
        line = 'mcp send_command {"commands":"left:up&forward:0.01 | right:hold","wait":true}'
        command.value = line
        await pilot.press("tab")
        assert command.value == line
        await pilot.press("enter")
        await asyncio.to_thread(until, lambda: not router.pending)
        await pilot.pause(.08)
        assert controller.status()["control_owner"] == "policy"
        assert any("read-only" in entry["message"] for entry in controller.read_events()["events"])
        await pilot.press("up")
        assert command.value == line
        command.value = 'mcp state {"broken":'
        await pilot.press("enter")
        assert app.is_running and app.query_one(StructuredLog).records
        command.value = "help mcp"
        await pilot.press("enter")
        assert any("mcp send_command" in event["message"] for event in controller.read_events()["events"])
        command.value = "exit"
        await pilot.press("enter")
        await asyncio.to_thread(until, lambda: controller.done.is_set())
        await pilot.pause(.08)
    assert app.return_code == 0, controller.read_events()['events'][-30:]


@pytest.mark.asyncio
async def test_tui_can_pause_and_exit_while_tool_waits(local_service):
    from robot_control.terminal_commands import TerminalCommandRouter
    from robot_control.tui import CommandInput, RobotControlApp

    controller, _, service = local_service
    local_call(service, "take")
    router = TerminalCommandRouter(controller, service)
    app = RobotControlApp(controller, start_controller=False, router=router)
    async with app.run_test() as pilot:
        command = app.query_one(CommandInput)
        command.value = 'mcp send_command {"commands":"left:up:1","wait":true,"timeout_s":120}'
        await pilot.press("enter")
        await asyncio.to_thread(until, lambda: controller.status()["direct"]["phase"] != "idle")
        assert router.pending
        command.value = "pause"
        await pilot.press("enter")
        await asyncio.to_thread(until, lambda: not router.pending)
        assert controller.status()["state"] == "paused"
        assert any('"cancelled"' in entry["message"] for entry in controller.read_events()["events"])
        command.value = 'mcp state {"wait_for_new":true,"since_seq":1000000000000,"timeout_s":120}'
        await pilot.press("enter")
        assert router.pending
        await pilot.press("ctrl+c")
        await asyncio.to_thread(until, lambda: controller.done.is_set() and not router.pending)
        await pilot.pause(.08)
    assert app.return_code == 0, controller.read_events()['events'][-30:]


def test_headless_handles_errors_and_waits_without_blocking_input(local_service):
    from robot_control.cli import run_headless
    from robot_control.terminal_commands import TerminalCommandRouter

    controller, _, service = local_service
    router = TerminalCommandRouter(controller, service)
    output = io.StringIO()
    proceed = threading.Event()

    class Input:
        def __iter__(self):
            yield 'mcp state {"broken":'
            yield "mcp unknown-tool"
            yield "mcp props"
            assert proceed.wait(3)
            yield "exit"

    def observe_results():
        until(lambda: any("MCP props:" in item["message"] for item in controller.read_events()["events"]))
        proceed.set()

    observer = threading.Thread(target=observe_results)
    observer.start()
    try:
        run_headless(controller, stdin=Input(), stdout=output, router=router)
    finally:
        proceed.set()
        observer.join(3)
    text = output.getvalue()
    assert "MCP props:" in text
    assert "unknown-tool" in text and "ERROR" in text.upper()
    assert "命令失败" in text
    assert controller.done.is_set() and not router.pending


def test_cli_wires_router_to_headless_and_mcp_quit_cleans_up(setup_controller, monkeypatch):
    from robot_control import cli
    from robot_control.mcp_server import MCPService

    controller, robot, _ = setup_controller
    service = MCPService(controller, {"port": 0}, allow_ephemeral=True)
    monkeypatch.setattr(cli, "make_controller", lambda path: controller)
    monkeypatch.setattr(cli, "make_mcp_service", lambda *args: service)

    class Input:
        def __iter__(self):
            until(lambda: controller.status()["policy"] == "first" and controller.status()["state"] == "paused")
            yield "mcp take"
            until(lambda: any("MCP take:" in item["message"] and '"succeeded"' in item["message"]
                              for item in controller.read_events()["events"]))
            yield "mcp command quit"
            # Keep stdin open, like an interactive session. Remote/local quit
            # must terminate the CLI without requiring another line or EOF.
            assert controller.done.wait(3)

    monkeypatch.setattr(cli.sys, "stdin", Input())
    assert cli.main(["--headless"]) == 0
    assert controller.done.is_set() and robot.closed
    assert not service.thread.is_alive()
    assert any('"accepted"' in item["message"] for item in controller.read_events()["events"])
