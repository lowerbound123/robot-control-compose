from concurrent.futures import Future

import pytest


@pytest.mark.parametrize(("line", "expected"), [
    ("mcp take", ("take", {})),
    ("  mcp\tstate  ", ("state", {})),
    ("mcp show rc", ("show", {"scope": "rc"})),
    ("mcp get_operation op-12", ("get_operation", {"operation_id": "op-12"})),
    ("mcp send_command left:up&forward:0.01 | right:hold",
     ("send_command", {"commands": "left:up&forward:0.01 | right:hold"})),
    ('mcp command set policy.params.label "你好  [bold]"',
     ("command", {"line": 'set policy.params.label "你好  [bold]"'})),
    ('mcp send_command {"commands":["left:open","right:close"],"wait":true}',
     ("send_command", {"commands": ["left:open", "right:close"], "wait": True})),
    ('mcp move_eef {"targets":{"left":{"end_pos":[0,0,0,0,0,0]}}}',
     ("move_eef", {"targets": {"left": {"end_pos": [0, 0, 0, 0, 0, 0]}}})),
])
def test_parse_mcp_preserves_arguments(line, expected):
    from robot_control.terminal_commands import parse_mcp_command
    assert parse_mcp_command(line) == expected


@pytest.mark.parametrize("line", [
    "mcp", "status", "mcp state true", "mcp state []",
    'mcp send_command {"commands":', 'mcp state {"wait_for_new":NaN}',
])
def test_parse_mcp_rejects_invalid_argument_forms(line):
    from robot_control.terminal_commands import parse_mcp_command
    with pytest.raises(ValueError):
        parse_mcp_command(line)


class RecordingController:
    def __init__(self):
        self.lines = []
        self.logs = []

    def submit(self, line):
        self.lines.append(line)
        return "ordinary-operation"

    def _emit(self, message, level="info"):
        self.logs.append((level, message))


class DeferredService:
    tools = ({"name": "send_command", "description": "Move arms.", "input_schema": {
        "properties": {"commands": {"type": "string"}, "wait": {"type": "boolean", "default": False}},
        "required": ["commands"],
    }},)

    def __init__(self):
        self.calls = []
        self.future = Future()

    def submit_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.future


def test_router_reports_receipt_without_dumping_image_content():
    from mcp.types import ImageContent
    from robot_control.mcp_server import result
    from robot_control.terminal_commands import TerminalCommandRouter

    controller, service = RecordingController(), DeferredService()
    router = TerminalCommandRouter(controller, service)
    future = router.submit('mcp send_command {"commands":"left:open","include_images":true}')
    assert future is service.future and not future.done()
    assert router.pending
    service.future.set_result(result(
        {"operation_id": "motion-1", "status": "running", "images": [{"camera": "head"}]},
        [ImageContent(type="image", mime_type="image/jpeg", data="DO_NOT_LOG_BASE64")],
    ))
    assert not router.pending
    output = "\n".join(message for _, message in controller.logs)
    assert "send_command" in output and "motion-1" in output and '"running"' in output
    assert "head" in output and "DO_NOT_LOG_BASE64" not in output
    assert "succeeded" not in output
    assert service.calls == [("send_command", {"commands": "left:open", "include_images": True})]


@pytest.mark.parametrize("failure", ["result", "exception", "cancel"])
def test_router_reports_async_errors_and_cleans_pending(failure):
    from robot_control.mcp_server import result
    from robot_control.terminal_commands import TerminalCommandRouter

    controller, service = RecordingController(), DeferredService()
    router = TerminalCommandRouter(controller, service)
    router.submit("mcp send_command left:open")
    if failure == "result":
        service.future.set_result(result({"error": "read-only"}, error=True))
    elif failure == "exception":
        service.future.set_exception(ValueError("invalid argument"))
    else:
        router.close()
        assert service.future.cancelled()
    assert not router.pending
    assert any(level in ("error", "warning") for level, _ in controller.logs)
    # A failed MCP call must not prevent ordinary controller interrupts.
    assert router.submit("pause") == "ordinary-operation"
    assert controller.lines == ["pause"]


@pytest.mark.parametrize('reply', [True, False])
def test_shutdown_receipt_gets_bounded_grace_before_local_cancel(reply):
    import time
    from robot_control.mcp_server import result
    from robot_control.terminal_commands import TerminalCommandRouter
    controller, service = RecordingController(), DeferredService()
    router = TerminalCommandRouter(controller, service)
    router.submit('mcp command quit')
    router.close()
    assert not service.future.cancelled()
    if reply:
        service.future.set_result(result({'status':'accepted'}))
        assert not router.pending
        assert any('accepted' in text for _, text in controller.logs)
    else:
        deadline = time.monotonic() + 2
        while router.pending and time.monotonic() < deadline:
            time.sleep(.01)
        assert service.future.cancelled() and not router.pending


def test_router_local_help_is_distinct_from_mcp_help():
    from robot_control.terminal_commands import TerminalCommandRouter

    controller, service = RecordingController(), DeferredService()
    router = TerminalCommandRouter(controller, service)
    for line in ("mcp", "help mcp", "help"):
        router.submit(line)
    assert controller.lines == ["help"]
    assert service.calls == []
    output = "\n".join(message for _, message in controller.logs)
    assert "send_command" in output and "commands" in output and "wait" in output
    assert "left:up" in output and "take" in output
    router.submit("mcp help")
    assert service.calls == [("help", {})]
    router.close()


def test_mcp_name_cannot_be_declared_as_policy_extension():
    from rc_policy_sdk.commands import command_specs
    with pytest.raises(ValueError, match="reserved"):
        command_specs({"tui_commands": {"mcp": {"handler": "mcp", "args": []}}})
