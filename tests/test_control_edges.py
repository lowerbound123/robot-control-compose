import time
import threading

import numpy as np
import pytest

from test_controller import setup_controller as setup_controller, ready, until
from test_control_ownership import call
from robot_control.direct_commands import parse_commands


@pytest.mark.parametrize("line", ["reset", "save-config policy", "exit"])
def test_all_mcp_writes_are_readonly_until_take(setup_controller, line):
    controller, _, _ = setup_controller
    ready(controller)
    with pytest.raises(PermissionError):
        controller.submit(line, origin="mcp")
    assert not controller._stop.is_set()


def test_return_waits_for_old_request_and_repeated_return_shares_receipt(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    controller.submit("resume")
    until(lambda: len(worker.requests) == 1)
    call(controller, "take")
    first = controller.submit("return", origin="mcp")
    until(lambda: controller.status()["returning"])
    second = controller.submit("return", origin="mcp")
    assert second == first
    assert controller.get_operation(first)["status"] not in ("succeeded", "failed")
    with pytest.raises(PermissionError):
        controller.submit("reset", origin="mcp")
    worker.finish(np.zeros((2, 14)))
    assert controller.wait_operation(first, 2)["status"] == "succeeded"
    assert controller.status()["consumed"] == 0


def test_late_error_does_not_interrupt_manual_control(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    controller.submit("resume")
    until(lambda: len(worker.requests) == 1)
    call(controller, "take")
    request = worker.requests[-1]
    worker.responses.put({"type": "error", "request_id": request["request_id"], "error": "old inference failed"})
    until(lambda: worker.closed)
    assert controller.status()["control_owner"] == "mcp"
    until(lambda: controller._operation is None)
    move = controller.submit_direct(parse_commands("left:open:-3.4"))
    assert controller.wait_operation(move, 2)["status"] == "succeeded"


def test_return_reset_failure_is_readonly_and_paused(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    call(controller, "take")
    def fail(**kwargs):
        raise RuntimeError("reset rejected")
    worker.reset_context = fail
    assert call(controller, "return")["status"] == "failed"
    until(lambda: controller._operation is None)
    assert controller.status()["state"] == "paused"
    assert controller.status()["control_owner"] == "policy"


def test_direct_batch_relative_accumulation_and_clear_queue(setup_controller):
    controller, robot, _ = setup_controller
    ready(controller)
    call(controller, "take")
    steps = parse_commands(["left:up:0.01@fast", "left:up:0.01@fast"])
    op = controller.submit_direct(steps)
    assert controller.wait_operation(op, 2)["status"] == "succeeded"
    assert robot.read_state()["eef"][2] == pytest.approx(.02)
    first = controller.submit_direct(parse_commands(["left:up:0.02@slow", "left:up:1@slow"]))
    until(lambda: controller.direct.active is not None)
    second = controller.submit_direct(parse_commands("right:open"), clear_queue=True)
    result = controller.wait_operation(first, 3)
    assert result["status"] == "cancelled"
    assert result["result"]["completed_steps"] == 1
    assert result["result"]["cancelled_steps"] == 1
    assert controller.wait_operation(second, 2)["status"] == "succeeded"


def test_wait_timeout_does_not_cancel_and_old_samples_never_complete(setup_controller):
    controller, robot, _ = setup_controller
    worker = ready(controller)
    call(controller, "take")
    controller.direct.config["step_timeout_s"] = .2
    robot.feedback_since = lambda arm, seq: []
    op = controller.submit_direct(parse_commands("left:close"))
    assert controller.wait_operation(op, 0)["status"] in ("queued", "running")
    assert controller.wait_operation(op, 2)["status"] == "failed"
    assert controller.status()["state"] == "fault"
    until(lambda: controller._operation is None)
    assert not worker.closed, "manual failure must retain the paused policy model"


def test_pause_cancels_deferred_return_receipt_and_allows_take_again(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    controller.submit("resume")
    until(lambda: len(worker.requests) == 1)
    call(controller, "take")
    returning = controller.submit("return", origin="mcp")
    until(lambda: controller._operation is not None and controller._operation["kind"] == "reset_context")
    pause = controller.submit("pause")
    assert controller.wait_operation(pause, 2)["status"] == "succeeded"
    assert controller.wait_operation(returning, .1)["status"] == "cancelled"
    assert not controller.status()["returning"]
    assert call(controller, "take")["status"] == "succeeded"
    worker.finish(np.zeros((1, 14)))


def test_partial_publish_failure_does_not_consume_and_is_not_retried(setup_controller):
    controller, robot, _ = setup_controller
    worker = ready(controller)
    controller.submit("resume")
    until(lambda: len(worker.requests) == 1)
    count = []
    def broken(values):
        count.append(values)
        raise RuntimeError("right publish failed; already published arms=['left']")
    robot.execute = broken
    worker.finish(np.ones((2, 14)))
    until(lambda: controller.status()["state"] == "fault")
    assert controller.status()["consumed"] == 0
    time.sleep(.03)
    assert len(count) == 1


@pytest.mark.parametrize("line", ["pause extra", "load nonexistent"])
def test_rejected_command_does_not_cancel_accepted_motion(setup_controller, line):
    controller, _, _ = setup_controller
    ready(controller)
    call(controller, "take")
    move = controller.submit_direct(parse_commands("left:up:0.02@slow"))
    until(lambda: controller.direct.active is not None)
    assert call(controller, line)["status"] == "failed"
    assert controller.wait_operation(move, 2)["status"] == "succeeded"


def test_pause_cancels_direct_requests_not_yet_dispatched(setup_controller):
    controller, _, _ = setup_controller
    ready(controller)
    call(controller, "take")
    with controller._lock:
        motion = controller.submit_direct(parse_commands("left:up:0.02@slow"))
        pause = controller.submit("pause")
    assert controller.wait_operation(pause, 2)["status"] == "succeeded"
    assert controller.wait_operation(motion, 2)["status"] == "cancelled"


def test_full_command_queue_cannot_block_exit(setup_controller):
    controller, _, _ = setup_controller
    ready(controller)
    with controller._lock:
        for _ in range(controller.operations.max_pending):
            controller.submit("set policy.params.value 2")
        exit_operation = controller.submit("exit")
    assert controller.done.wait(2)
    assert controller.get_operation(exit_operation)["status"] == "succeeded"


def test_local_reset_waits_for_return_lifecycle_and_remains_paused(setup_controller):
    controller, _, _ = setup_controller
    worker = ready(controller)
    controller.submit("resume")
    until(lambda: len(worker.requests) == 1)
    call(controller, "take")
    returning = controller.submit("return", origin="mcp")
    until(lambda: controller._operation is not None)
    resetting = controller.submit("reset")
    assert controller.wait_operation(resetting, .05)["status"] == "queued"
    worker.finish(np.zeros((1, 14)))
    assert controller.wait_operation(returning, 2)["status"] == "succeeded"
    assert controller.wait_operation(resetting, 2)["status"] == "succeeded"
    assert controller.status()["state"] == "paused"


def test_return_feedback_failure_finishes_receipt_and_can_recover(setup_controller):
    controller, robot, _ = setup_controller
    worker = ready(controller)
    call(controller, "take")
    lost = {"feedback": False}
    read_state, reset_context = robot.read_state, worker.reset_context
    def read():
        if lost["feedback"]:
            raise RuntimeError("stale feedback after reset_context")
        return read_state()
    def reset(**kwargs):
        reset_context(**kwargs)
        lost["feedback"] = True
    robot.read_state, worker.reset_context = read, reset
    operation = controller.submit("return", origin="mcp")
    result = controller.wait_operation(operation, .3)
    assert result["status"] == "failed", result
    assert controller.status()["state"] == "paused"
    assert controller.status()["control_owner"] == "policy"
    assert not controller.status()["returning"] and not worker.closed
    lost["feedback"] = False
    worker.reset_context = reset_context
    assert call(controller, "take")["status"] == "succeeded"
    assert call(controller, "return")["status"] == "succeeded"


def test_queued_take_cannot_undo_return_readonly_gate(setup_controller):
    controller, _, _ = setup_controller
    ready(controller)
    with controller._lock:
        take = controller.submit("take", origin="mcp")
        for _ in range(20):
            controller.submit("take", origin="mcp")
        returning = controller.submit("return", origin="mcp")
    assert controller.wait_operation(take, 1)["status"] == "succeeded"
    with pytest.raises(PermissionError):
        controller.submit_direct(parse_commands("left:up:0.01@fast"))
    assert controller.wait_operation(returning, 2)["status"] == "succeeded"


def test_pending_return_stops_advancing_active_manual_trajectory(setup_controller):
    controller, robot, _ = setup_controller
    ready(controller)
    call(controller, "take")
    motion = controller.submit_direct(parse_commands("left:up:1@slow"))
    until(lambda: controller.direct.active is not None)
    with controller._lock:
        before = robot.read_state()["eef"][2]
        for _ in range(20):
            controller.submit("take", origin="mcp")
        returning = controller.submit("return", origin="mcp")
    assert controller.wait_operation(returning, 2)["status"] == "succeeded"
    assert controller.get_operation(motion)["status"] == "cancelled"
    assert robot.read_state()["eef"][2] == pytest.approx(before, abs=1e-9, rel=0)


def test_mixed_policy_publishes_modes_on_control_thread(setup_controller):
    controller, robot, _ = setup_controller
    worker = ready(controller)
    record = controller.registry.get("first")
    record.config["execution"].update(control_mode="mixed", call_every_actions=10)
    threads = []
    original = robot.execute_arms
    def record_publish(targets):
        threads.append(threading.current_thread().name)
        original(targets)
    robot.execute_arms = record_publish
    controller.submit("resume")
    until(lambda: len(worker.requests) == 1)
    request = worker.requests[-1]
    assert request["action_chunk"] == {"actions": [], "modes": []}
    worker.responses.put({"type": "result", "images_released": True, **{key: request[key] for key in ("request_id", "generation", "config_version")},
                          "actions": [[0] * 14, [.1] * 14, [.2] * 14], "modes": ["qpos", "epos", "qpos"]})
    until(lambda: controller.status()["consumed"] == 3)
    assert any(mode == {"left": "epos", "right": "epos"} for mode in robot.command_modes)
    assert set(threads) == {"rc-control"}
