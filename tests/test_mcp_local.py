import asyncio
import json
from queue import Empty
import time

import pytest

from test_controller import setup_controller as setup_controller, ready, until
from test_mcp import client


@pytest.fixture
def local_service(setup_controller):
    from robot_control.mcp_server import MCPService
    controller, robot, _ = setup_controller
    ready(controller)
    service = MCPService(controller, {"port": 0}, allow_ephemeral=True)
    service.start()
    try:
        yield controller, robot, service
    finally:
        service.close()


def local_call(service, name, arguments=None):
    return service.submit_tool(name, arguments or {}).result(timeout=3)


def test_local_and_http_share_discovery_validation_and_control(local_service):
    controller, robot, service = local_service

    async def scenario():
        async with client(service.url) as session:
            assert {item["name"] for item in service.tools} == {
                tool.name for tool in (await session.list_tools()).tools
            }
            for name, args in (
                ("props", {}), ("show", {"scope": "rc"}),
                ("send_command", {"commands": "left:up:0.01"}),
                ("move_eef", {"targets": {"left": {"end_pos": [0, 0]}}}),
                ("show", {}),
            ):
                remote = await session.call_tool(name, args)
                local = local_call(service, name, args)
                assert local.is_error == remote.is_error
                if not local.is_error:
                    # HTTP serializes tuples (e.g. props.motions) as JSON arrays.
                    assert json.loads(json.dumps(local.structured_content)) == remote.structured_content
            assert not local_call(service, "take").is_error
            assert controller.status()["control_owner"] == "mcp"
            remote = await session.call_tool("send_command", {"commands": "left:up:0.01", "wait": True})
            assert remote.structured_content["status"] == "succeeded"
            local = local_call(service, "move_eef", {
                "targets": {"left": {"end_pos": [0, 0, .02, 0, 0, 0]}}, "wait": True,
            })
            assert local.structured_content["status"] == "succeeded"
            assert robot.read_state()["eef"][2] == pytest.approx(.02)
            receipt = local_call(service, "get_operation", {
                "operation_id": local.structured_content["operation_id"],
            })
            assert receipt.structured_content["status"] == "succeeded"
            assert not local_call(service, "return", {"wait": True}).is_error
            assert controller.status()["control_owner"] == "policy"
    asyncio.run(scenario())


def test_local_wait_does_not_block_return_and_timeout_does_not_cancel(local_service):
    controller, _, service = local_service
    local_call(service, "take")
    movement = local_call(service, "send_command", {"commands": "left:up:1", "wait": True, "timeout_s": .01})
    identity = movement.structured_content["operation_id"]
    assert movement.structured_content["status"] in ("queued", "running")
    waiting = service.submit_tool("get_operation", {"operation_id": identity, "wait": True, "timeout_s": 120})
    until(lambda: controller.get_operation(identity)["status"] == "running")
    assert not waiting.done()
    assert not local_call(service, "return", {"wait": True}).is_error
    assert waiting.result(2).structured_content["status"] == "cancelled"


def test_local_unknown_tool_and_nested_terminal_command_fail(local_service):
    controller, _, service = local_service
    unknown = service.submit_tool("unknown-tool", {})
    try:
        response = unknown.result(2)
    except Exception as exc:
        assert "unknown" in str(exc).lower()
    else:
        assert response.is_error
    for taken in (False, True):
        if taken:
            local_call(service, "take")
        response = local_call(service, "command", {"line": "mcp take", "wait": True})
        assert response.is_error
        assert "local-only" in response.structured_content["error"]
        response = local_call(service, "command", {"line": "custom-policy-command", "wait": True})
        assert response.is_error
    assert not controller.done.is_set()


def test_service_close_cancels_local_waits_without_waiting_for_timeout(local_service):
    controller, _, service = local_service
    local_call(service, "take")
    movement = local_call(service, "send_command", {"commands": "left:up:1"})
    identity = movement.structured_content["operation_id"]
    waiting = service.submit_tool("get_operation", {"operation_id": identity, "wait": True, "timeout_s": 120})
    state = service.submit_tool("state", {"wait_for_new": True, "since_seq": 10**12, "timeout_s": 120})
    # A later query completing proves both waits reached the event loop.
    local_call(service, "props")
    assert not waiting.done() and not state.done()
    start = time.monotonic()
    service.close()
    assert time.monotonic() - start < 2
    assert waiting.cancelled() and state.cancelled()
    assert not service.thread.is_alive()
    # Cancelling a wait is not an implicit robot action/return.
    assert controller.get_operation(identity)["status"] in ("queued", "running")
    with pytest.raises(RuntimeError):
        service.submit_tool("take", {})


def test_events_results_stay_visible_without_becoming_query_history(local_service):
    from robot_control.terminal_commands import TerminalCommandRouter

    controller, _, service = local_service
    router = TerminalCommandRouter(controller, service)
    controller._emit("original diagnostic")
    baseline = controller.read_events()
    for _ in range(3):
        response = router.submit("mcp events").result(2)
        until(lambda: not router.pending)
        assert response.structured_content == baseline
        assert controller.read_events() == baseline
    messages = []
    while True:
        try:
            messages.append(controller.events.get_nowait()["message"])
        except Empty:
            break
    queries = [message for message in messages if message.startswith("MCP events:")]
    assert len(queries) == 3 and all("original diagnostic" in message for message in queries)
    log = controller.outputs / "logs" / f"rc_{controller.run_id}.jsonl"
    assert sum(json.loads(line)["message"].startswith("MCP events:") for line in log.read_text().splitlines()) == 3
    assert not router.submit("mcp props").result(2).is_error
    until(lambda: not router.pending)
    assert any(item["message"].startswith("MCP props:") for item in controller.read_events()["events"])
