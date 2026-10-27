import asyncio
import json
import sys
import time

import pytest

from robot_control.controller import RobotController
from host_helpers import LocalPolicyProcess as PolicyProcess
from robot_control.robots import MockRobot
from test_controller import until
from test_command_worker import SOURCE, worker_config


@pytest.fixture
def command_controller(tmp_path, monkeypatch, request):
    for name in ("first", "second"):
        directory = tmp_path / "policy" / name
        directory.mkdir(parents=True)
        (directory / "main.py").write_text(getattr(request, "param", SOURCE))
        (directory / "main.py").with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
        config = worker_config(directory, workers=2)
        config.update(schema_version=3, id=name, hot_reloadable=["params.value"])
        config["params"]["value"] = 1
        config["execution"].update(mode="sync", call_every_actions=1)
        (directory / "policy.json").write_text(json.dumps(config))
    config = {"schema_version": 3, "frequency_hz": 100, "policy_directory": "policy",
              "startup_policy": "first", "robot": {"backend": "mock"},
              "safety": {"enabled": False, "reference_frequency_hz": 50, "max_delta_per_step": [.1]*14},
              "reset_qpos": [0]*14, "outputs_directory": "outputs",
              # These tests exercise commands, not cold Python/NumPy startup
              # deadlines; allow container CPU scheduling during suite setup.
              "timeouts": {"initialize_s": 10, "inference_s": 3, "release_s": .2}}
    path = tmp_path / "rc.json"
    path.write_text(json.dumps(config))
    def factory(*args, **kwargs):
        return PolicyProcess(*args, python_command=[sys.executable], **kwargs)
    controller = RobotController(path, robot=MockRobot(), process_factory=factory)
    controller.start()
    try:
        try:
            until(lambda: controller.status()["policy"] == "first" and controller.status()["state"] == "paused", 15)
        except AssertionError as exc:
            raise AssertionError(f"{controller.status()} {controller.read_events()}") from exc
        yield controller, tmp_path
    finally:
        controller.submit("exit")
        assert controller.done.wait(10)


def completed(controller, identity, timeout=5):
    until(lambda: controller.get_operation(identity)["status"] in ("succeeded", "failed", "cancelled"), timeout)
    return controller.get_operation(identity)


def test_local_commands_during_inference_preserve_generation_and_consume(command_controller):
    controller, _ = command_controller
    controller.submit("resume")
    until(lambda: controller.status()["in_flight"])
    generation = controller.status()["generation"]
    first = controller.submit('meet "first value"')
    second = controller.submit("meet second")
    assert completed(controller, first)["result"] == "first value"
    assert completed(controller, second)["result"] == "second"
    assert controller.status()["generation"] == generation
    until(lambda: controller.status()["consumed"] >= 1)
    controller.submit("pause")


def test_help_and_error_isolation(command_controller):
    controller, _ = command_controller
    assert "meet <str>" in controller.local_help()
    help_id = controller.submit("help delay")
    assert "0.15" in completed(controller, help_id)["result"]
    bad = controller.submit("bad")
    ok = controller.submit('unlimited "with spaces" .02')
    assert completed(controller, bad)["status"] == "failed"
    assert completed(controller, ok)["result"] == "with spaces"
    assert controller.status()["state"] == "paused"
    invalid = controller.submit("delay label not-a-float")
    assert completed(controller, invalid)["status"] == "failed"
    assert controller._worker.alive


def test_timeout_unloads_worker_cancels_peers_and_requires_switch(command_controller):
    controller, _ = command_controller
    worker = controller._worker
    slow = controller.submit("delay slow .8")
    peer = controller.submit("unlimited peer 1")
    assert completed(controller, slow)["status"] == "failed"
    assert "timeout" in controller.get_operation(slow)["error"]
    assert completed(controller, peer)["status"] == "cancelled"
    until(lambda: controller._worker is None, 8)
    assert not worker.alive
    assert controller.status()["state"] == "paused"
    assert controller.local_commands() == []
    assert completed(controller, controller.submit("resume"))["status"] == "failed"


def test_queued_command_cannot_run_on_replacement_worker(command_controller):
    controller, root = command_controller
    with controller._lock:
        switch = controller.submit("switch second")
        old = controller.submit("unlimited stale .01")
    assert completed(controller, switch)["status"] == "succeeded"
    assert completed(controller, old)["status"] == "cancelled"
    assert not (root / "policy" / "second" / "stale.start").exists()


def test_pause_and_take_do_not_cancel_active_command(command_controller):
    controller, root = command_controller
    task = controller.submit("unlimited active .25")
    until(lambda: (root / "policy" / "first" / "active.start").exists())
    controller.submit("pause")
    take = controller.submit("take")
    assert completed(controller, take)["status"] == "succeeded"
    assert completed(controller, task)["status"] == "succeeded"
    assert controller.status()["control_owner"] == "mcp"


def test_mcp_cannot_invoke_custom_commands_before_or_after_take(command_controller):
    from robot_control.mcp_server import MCPService
    from test_mcp import client
    controller, root = command_controller
    service = MCPService(controller, {"port": 0}, allow_ephemeral=True)
    service.start()
    try:
        async def scenario():
            async with client(service.url) as session:
                for takeover in (False, True):
                    if takeover:
                        await session.call_tool("take", {})
                    result = await session.call_tool("command", {"line": "unlimited forbidden .01", "wait": True})
                    assert result.is_error
                    help_result = await session.call_tool("help", {})
                    assert "unlimited" not in str(help_result)
                assert not (root / "policy" / "first" / "forbidden.start").exists()
        asyncio.run(scenario())
    finally:
        service.close()


def test_timeout_during_return_cannot_leave_return_receipt_running(command_controller):
    controller, root = command_controller
    completed(controller, controller.submit("take"))
    task = controller.submit("delay active .8")
    until(lambda: (root / "policy" / "first" / "active.start").exists())
    returning = controller.submit("return")
    assert completed(controller, task)["status"] == "failed"
    assert completed(controller, returning)["status"] in ("failed", "cancelled")
    until(lambda: controller._worker is None, 8)
    assert controller.status()["state"] == "paused"
    assert controller.status()["returning"] is False


def test_timeout_during_mcp_motion_preserves_owner_and_motion(command_controller):
    from robot_control.direct_commands import structured_step
    controller, _ = command_controller
    completed(controller, controller.submit("take"))
    # A sufficiently distant target stays active while the model is unloaded.
    motion = controller.submit_direct([structured_step(4, {"left": {"end_pos": [.3, 0, 0, 0, 0, 0]}}, "slow")])
    task = controller.submit("delay slow .4")
    assert completed(controller, task)["status"] == "failed"
    until(lambda: controller._worker is None, 8)
    assert controller.status()["control_owner"] == "mcp"
    assert controller.get_operation(motion)["status"] == "running", str(controller.get_operation(motion))


def test_shutdown_cancels_queued_and_bounds_unlimited_active_commands(command_controller):
    controller, root = command_controller
    calls = [controller.submit(f"unlimited hang{i} 30") for i in range(3)]
    until(lambda: (root / "policy" / "first" / "hang1.start").exists())
    worker = controller._worker
    started = time.monotonic()
    controller.submit("exit")
    assert controller.done.wait(9)
    assert time.monotonic() - started < 9
    assert not worker.alive
    assert not (root / "policy" / "first" / "hang2.start").exists()
    assert all(controller.get_operation(identity)["status"] == "cancelled" for identity in calls)


def test_headless_calls_configured_command(command_controller):
    import io
    from robot_control.cli import run_headless
    controller, _ = command_controller
    output = io.StringIO()
    class Input:
        def __iter__(self):
            yield 'unlimited "local stdin" .01\n'
            until(lambda: any('succeeded: "local stdin"' in event['message'] for event in controller.read_events()['events']))
            yield 'exit\n'
    run_headless(controller, stdin=Input(), stdout=output)
    assert 'succeeded: "local stdin"' in output.getvalue()


def test_worker_crash_settles_call_and_removes_commands(command_controller):
    controller, _ = command_controller
    call = controller.submit("die")
    assert completed(controller, call)["status"] == "failed"
    until(lambda: controller._worker is None, 5)
    assert controller.local_commands() == []


@pytest.mark.parametrize("command_controller", [SOURCE.replace('context["barrier"].wait(3)\n    return [[0] * 14]', 'return [[0] * 14]')], indirect=True)
def test_timeout_during_running_policy_stops_consuming(command_controller):
    controller, _ = command_controller
    completed(controller, controller.submit("resume"))
    until(lambda: controller.status()["consumed"] > 2)
    task = controller.submit("delay slow .5")
    assert completed(controller, task)["status"] == "failed"
    until(lambda: controller._worker is None, 8)
    before = controller.status()["consumed"]
    time.sleep(.05)
    assert controller.status()["consumed"] == before
    assert controller.status()["state"] == "paused"


def test_timeout_does_not_reopen_writes_for_queued_return(command_controller, monkeypatch):
    controller, root = command_controller
    completed(controller, controller.submit("take"))
    task = controller.submit("delay slow .6")
    until(lambda: (root / "policy" / "first" / "slow.start").exists())
    snapshots = []
    original = controller._poll_policy_commands
    def poll():
        original()
        if controller.get_operation(task)["status"] == "failed" and not snapshots:
            snapshots.append(controller.control_owner != "mcp" or controller._return_requested)
    monkeypatch.setattr(controller, "_poll_policy_commands", poll)
    with controller._lock:
        controller.submit("take")
        returning = controller.submit("return")
        time.sleep(.2)
    assert completed(controller, returning)["status"] == "failed"
    assert snapshots == [True]
    assert controller.status()["control_owner"] == "policy"
    with pytest.raises(PermissionError):
        controller.submit("reset", origin="mcp")


@pytest.mark.parametrize("line", ["resume", "set policy.params.value 2"])
def test_pause_of_deferred_lifecycle_reopens_command_gate(command_controller, line):
    controller, _ = command_controller
    completed(controller, controller.submit("resume"))
    until(lambda: controller.status()["in_flight"])
    lifecycle = controller.submit(line)
    until(lambda: controller._operation is not None and not controller._operation["launched"])
    completed(controller, controller.submit("pause"))
    assert completed(controller, lifecycle)["status"] == "cancelled"
    # Both commands must reach the still-running inference's three-party barrier.
    first, second = controller.submit("meet first"), controller.submit("meet second")
    assert completed(controller, first)["result"] == "first"
    assert completed(controller, second)["result"] == "second"
    assert controller.status()["state"] == "paused"


def test_switch_preflight_failure_reopens_old_worker_commands(command_controller, monkeypatch):
    controller, _ = command_controller
    worker = controller._worker
    def invalid(*args): raise ValueError("environment unavailable")
    monkeypatch.setattr(controller.registry, "snapshot", invalid)
    assert completed(controller, controller.submit("switch second"))["status"] == "failed"
    call = controller.submit("unlimited recovered .01")
    assert completed(controller, call)["result"] == "recovered"
    assert controller._worker is worker
    assert controller.status()["state"] != "running"


def test_duplicate_json_result_only_fails_that_call(command_controller):
    controller, _ = command_controller
    worker = controller._worker
    call = controller.submit("duplicate")
    assert completed(controller, call)["status"] == "failed"
    following = controller.submit("unlimited healthy .01")
    assert completed(controller, following)["result"] == "healthy"
    assert controller._worker is worker and worker.alive


@pytest.mark.parametrize("name", ["huge_error", "surrogate_error", "broken_error"])
def test_exception_formatting_cannot_strand_a_command(command_controller, name):
    controller, _ = command_controller
    worker = controller._worker
    call = controller.submit(name)
    result = completed(controller, call, timeout=2)
    assert result["status"] == "failed"
    assert len(result["error"].encode("utf-8")) < 65536
    assert worker.alive
    following = controller.submit("unlimited healthy .01")
    assert completed(controller, following)["result"] == "healthy"


def test_local_direct_name_is_not_cancelled_as_mcp_motion(command_controller):
    controller, _ = command_controller
    with controller._lock:
        call = controller.submit("direct")
        controller.submit("pause")
    assert completed(controller, call)["result"] == "pong"
