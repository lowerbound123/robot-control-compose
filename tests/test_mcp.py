import asyncio
import socket
from contextlib import asynccontextmanager

import httpx2
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from test_controller import setup_controller as setup_controller, ready


@asynccontextmanager
async def client(url):
    async with httpx2.AsyncClient(trust_env=False) as http:
        async with streamable_http_client(url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session


def test_real_mcp_readonly_take_move_return_images_and_tui_commands(setup_controller):
    from robot_control.mcp_server import MCPService

    controller, _, _ = setup_controller
    ready(controller)
    service = MCPService(controller, {"host": "127.0.0.1", "port": 0}, allow_ephemeral=True)
    service.bind()
    service.start()
    try:
        async def scenario():
            async with client(service.url) as session:
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert {"take", "return", "props", "state", "command", "move_joints", "move_eef", "send_command", "get_operation", "events"} <= names
                denied = await session.call_tool("command", {"line": "reset"})
                assert denied.is_error
                assert not (await session.call_tool("take", {})).is_error
                shown = await session.call_tool("show", {"scope": "rc"})
                assert shown.structured_content["schema_version"] == 2
                movement = await session.call_tool("move_eef", {
                    "targets": {"left": {"end_pos": [.01, 0, 0, 0, 0, 0], "speed": "fast"}}, "wait": True})
                assert not movement.is_error, movement
                assert movement.structured_content["status"] == "succeeded"
                state = await session.call_tool("state", {"include_images": True, "cameras": ["head"]})
                assert len([block for block in state.content if block.type == "image"]) == 1
                assert state.structured_content["images"][0]["camera"] == "head"
                assert not (await session.call_tool("return", {"wait": True})).is_error
                assert (await session.call_tool("send_command", {"commands": "left:open"})).is_error
        asyncio.run(scenario())
    finally:
        service.close()


def test_mcp_bind_collision_fails_and_cleans_listener(setup_controller):
    from robot_control.mcp_server import MCPService
    controller, _, _ = setup_controller
    ready(controller)
    service = MCPService(controller, {"host": "127.0.0.1", "port": 0}, allow_ephemeral=True)
    service.bind()
    other = MCPService(controller, {"host": "127.0.0.1", "port": service.port})
    try:
        with pytest.raises(OSError):
            other.bind()
    finally:
        other.close()
        service.close()


def test_mcp_config_and_cli_override():
    from robot_control.mcp_config import mcp_settings
    assert mcp_settings()["port"] == 4000
    config = mcp_settings({"host": "localhost", "port": 4100}, "127.0.0.1:4200")
    assert (config["host"], config["port"]) == ("127.0.0.1", 4200)
    for address in ("http://localhost:4000/mcp", "localhost", ":4000", "localhost:0", "localhost:no"):
        with pytest.raises(ValueError):
            mcp_settings(override=address)


def test_invalid_camera_and_boolean_gripper_never_queue_motion(setup_controller):
    from robot_control.mcp_server import MCPService
    controller, robot, _ = setup_controller
    ready(controller)
    service = MCPService(controller, {"port": 0}, allow_ephemeral=True)
    service.start()
    try:
        async def scenario():
            async with client(service.url) as session:
                assert not (await session.call_tool("take", {})).is_error
                for extra in ({"cameras": ["missing"]}, {"targets": {"left": {"joint_pos": [0]*6, "gripper": True}}}):
                    response = await session.call_tool("move_joints", {"targets": {"left": {"joint_pos": [1]*6}}, "wait": True, **extra})
                    assert response.is_error
                assert controller.status()["direct"]["pending_steps"] == 0
                assert robot.read_state()["qpos"][6] == -5
        asyncio.run(scenario())
    finally:
        service.close()


def test_optional_token_protects_health_and_host_guard_protects_mcp(setup_controller, monkeypatch):
    from robot_control.mcp_server import MCPService
    controller, _, _ = setup_controller
    ready(controller)
    monkeypatch.setenv("RC_MCP_TEST_TOKEN", "local-test-token")
    service = MCPService(controller, {"port": 0, "token_env": "RC_MCP_TEST_TOKEN"}, allow_ephemeral=True)
    service.start()
    try:
        with httpx.Client(trust_env=False) as http:
            health = service.url.replace('/mcp', '/health')
            assert http.get(health).status_code == 401
            headers = {"Authorization": "Bearer local-test-token"}
            assert http.get(health, headers=headers).status_code == 200
            response = http.post(service.url, headers={**headers, "Host": "untrusted.example"}, json={})
            assert response.status_code == 421
    finally:
        service.close()


def test_cli_mcp_override_and_remote_shutdown_share_cleanup(setup_controller, monkeypatch):
    from robot_control import cli
    controller, robot, _ = setup_controller
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(cli, "make_controller", lambda path: controller)
    def run_ui(unused, *, router=None):
        async def scenario():
            async with client(f'http://127.0.0.1:{port}/mcp') as session:
                taken = await session.call_tool("take", {})
                assert not taken.is_error, taken
                response = await session.call_tool("command", {"line": "quit"})
                assert response.structured_content["status"] == "accepted"
        asyncio.run(scenario())
    monkeypatch.setattr(cli, "run_tui", run_ui)
    assert cli.main(["--mcp", f"127.0.0.1:{port}"]) == 0
    assert controller.done.is_set() and robot.closed
    with socket.socket() as check:
        assert check.connect_ex(('127.0.0.1', port)) != 0


def test_cli_busy_port_does_not_start_controller(setup_controller, monkeypatch):
    from robot_control import cli
    controller, robot, _ = setup_controller
    monkeypatch.setattr(cli, "make_controller", lambda path: controller)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        assert cli.main(["--mcp", f"127.0.0.1:{listener.getsockname()[1]}"]) == 1
    assert controller._thread is None
    assert controller.done.is_set() and robot.closed
