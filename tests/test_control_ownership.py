import numpy as np
import pytest

from test_controller import setup_controller as setup_controller, ready, until


def call(controller, line):
    identity = controller.submit(line, origin="mcp")
    return controller.wait_operation(identity, 2)


def test_default_readonly_and_take_return_with_inflight(setup_controller):
    controller, robot, _ = setup_controller
    worker = ready(controller)
    assert call(controller, "status")["result"]["control_owner"] == "policy"
    with pytest.raises(PermissionError):
        controller.submit("reset", origin="mcp")
    controller.submit("resume")
    until(lambda: len(worker.requests) == 1)
    assert call(controller, "take")["status"] == "succeeded"
    generation = controller.buffer.generation
    assert call(controller, "take")["status"] == "succeeded"
    assert controller.buffer.generation == generation
    worker.finish(np.ones((4, 14)))
    until(lambda: not controller.status()["in_flight"])
    assert controller.status()["consumed"] == 0
    assert len(worker.requests) == 1
    assert not worker.closed
    assert call(controller, "return")["status"] == "succeeded"
    until(lambda: len(worker.requests) == 2)
    assert controller.status()["control_owner"] == "policy"
    with pytest.raises(PermissionError):
        controller.submit("pause", origin="mcp")


def test_mutating_operations_report_real_result_and_resume_returns_owner(setup_controller):
    controller, _, path = setup_controller
    worker = ready(controller)
    call(controller, "take")
    result = call(controller, "set policy.params.value 7")
    assert result["status"] == "succeeded"
    assert controller.registry.get("first").version == 2
    assert call(controller, "save saved")["status"] == "succeeded"
    assert (path / "outputs/state_saves/saved.json").exists()
    assert call(controller, "set rc.frequency_hz 50")["status"] == "failed"
    assert call(controller, "resume")["status"] == "succeeded"
    until(lambda: len(worker.requests) == 1)
    assert controller.status()["control_owner"] == "policy"


def test_events_are_nonconsuming_and_invalidated_mcp_request_cannot_execute(setup_controller):
    controller, _, _ = setup_controller
    ready(controller)
    call(controller, "take")
    with controller._lock:
        write = controller.submit("set policy.params.value 42", origin="mcp")
        returned = controller.submit("return", origin="mcp")
    assert controller.wait_operation(returned, 2)["status"] == "succeeded"
    assert controller.wait_operation(write, 2)["status"] == "cancelled"
    assert controller.registry.get("first").config["params"]["value"] == 1
    before = controller.events.qsize()
    log = controller.read_events()
    assert log["events"] and controller.events.qsize() == before
    assert controller.read_events(after_seq=log["next_seq"])["events"] == []


def test_direct_control_requires_take_and_does_not_consume_policy(setup_controller):
    from robot_control.direct_commands import structured_step

    controller, robot, _ = setup_controller
    ready(controller)
    steps = [structured_step(4, {"left": {"end_pos": [.01, 0, 0, 0, 0, 0], "gripper": -3.4}}, "fast")]
    with pytest.raises(PermissionError):
        controller.submit_direct(steps)
    call(controller, "take")
    before = robot.read_state()["qpos"][7:]
    operation = controller.submit_direct(steps)
    result = controller.wait_operation(operation, 2)
    assert result["status"] == "succeeded", result
    assert controller.status()["consumed"] == 0
    assert robot.read_state()["eef"][:7] == pytest.approx([.01, 0, 0, 0, 0, 0, -3.4])
    assert robot.read_state()["qpos"][7:] == before
    assert controller.status()["control_owner"] == "mcp"


def test_return_cancels_active_direct_motion(setup_controller):
    from robot_control.direct_commands import structured_step

    controller, _, _ = setup_controller
    ready(controller)
    call(controller, "take")
    movement = controller.submit_direct([structured_step(4, {"left": {"end_pos": [1, 0, 0, 0, 0, 0]}}, "slow")])
    until(lambda: controller.get_operation(movement)["status"] == "running")
    assert call(controller, "return")["status"] == "succeeded"
    assert controller.get_operation(movement)["status"] == "cancelled"
