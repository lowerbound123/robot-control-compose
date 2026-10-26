import time

import pytest
from robot_control.log_tui import StructuredLog

from rc_policy_sdk.commands import command_specs
from robot_control.terminal_commands import TerminalCommandRouter
from robot_control.tui import CommandInput, RobotControlApp
from robot_control.policy_monitor_tui import PolicyMonitorPanel, PolicyMonitorSidebar
from test_tui import FakeController
from test_terminal_commands import RecordingController


def test_monitor_commands_are_reserved_local_and_have_help():
    with pytest.raises(ValueError, match="reserved"):
        command_specs({"tui_commands":{"monitor-A":{"handler":"f", "args":[]}}})
    controller = RecordingController()
    router = TerminalCommandRouter(controller)
    with pytest.raises(ValueError, match="TUI"):
        router.submit("monitor-A on")
    calls = []
    router.panel_handler = lambda name, desired: calls.append((name, desired))
    router.submit("monitor-A")
    router.submit("monitor-A off")
    assert calls == [("monitor-A", None), ("monitor-A", False)]
    with pytest.raises(ValueError): router.submit("monitor-A invalid")
    router.submit("help monitor-A")
    assert "monitor-" in controller.logs[-1][1]
    assert not controller.lines
    router.close()


class Monitors:
    def __init__(self): self.values = {}; self.closed = False
    def available(self): return ["A", "B"]
    def snapshot(self): return dict(self.values)
    def set_enabled(self, identity, desired):
        desired = identity not in self.values if desired is None else desired
        if desired:
            self.values.setdefault(identity, dict(values=None, status="waiting", updated_at=None, error=None))
        else: self.values.pop(identity, None)
        return desired
    def close(self, **kwargs): self.closed = True


@pytest.mark.parametrize("method", ["refresh_controller", "refresh_preview"])
def test_refresh_does_not_query_widgets_after_message_pump_stops(method):
    app = RobotControlApp(FakeController())
    # Textual stops the pump, then removes children, then emits App.Unmount.
    app._ui_active = True
    assert not app.is_running
    def forbidden_query(*args, **kwargs):
        raise AssertionError("timer queried a widget during teardown")
    app.query_one = forbidden_query
    getattr(app, method)()


@pytest.mark.asyncio
async def test_policy_panels_wrap_resize_scroll_and_restore_log_width():
    controller = FakeController()
    controller.policy_monitors = Monitors()
    app = RobotControlApp(controller)
    async with app.run_test(size=(120, 35)) as pilot:
        log = app.query_one(StructuredLog)
        full_width = log.size.width
        command = app.query_one(CommandInput)
        command.value = "monitor-A"
        await pilot.press("tab")
        assert command.value == "monitor-A "
        await pilot.press("enter")
        app.router.submit("monitor-B on")
        await pilot.pause(.15)
        sidebar = app.query_one(PolicyMonitorSidebar)
        panels = list(sidebar.query(PolicyMonitorPanel))
        assert len(panels) == 2
        assert log.region.right <= sidebar.region.x
        assert log.size.width < full_width
        initial = panels[0].size.height
        controller.policy_monitors.values["A"] = dict(values={"阶段":"[bold]" + "中文" * 120 + "\x1b[31m", "loss":1.25}, status="ready", updated_at=time.monotonic(), error=None)
        controller.policy_monitors.values["B"] = dict(values={"line":"x" * 3000}, status="ready", updated_at=time.monotonic(), error=None)
        await pilot.pause(.15)
        assert panels[0].size.height > initial
        assert "[bold]" in panels[0].content.plain and "\x1b" not in panels[0].content.plain
        assert sidebar.max_scroll_y > 0
        assert command.region.bottom <= app.size.height
        await pilot.resize_terminal(60, 24)
        await pilot.pause(.1)
        assert log.region.right <= sidebar.region.x
        assert panels[0].size.height > initial
        controller.policy_monitors.values["A"]["values"] = {"ok":1}
        await pilot.pause(.1)
        assert panels[0].size.height < 12
        app.router.submit("monitor-A off")
        app.router.submit("monitor-B off")
        await pilot.pause(.1)
        assert not sidebar.display
        assert log.size.width >= 55
        controller.done.set()
        await pilot.pause(.08)
    assert controller.policy_monitors.closed
