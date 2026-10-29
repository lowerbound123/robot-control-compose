from config_helpers import read_config
import json
import queue
import threading
import time

import numpy as np
import pytest

from robot_control.controller import RobotController
from robot_control.robots import MockRobot


def until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("controller condition timed out")
        time.sleep(0.005)


class FakeProcess:
    instances = []
    order = []
    hang_start = False

    def __init__(self, directory, config, version=1, **kwargs):
        self.config = config
        self.config_version = version
        self.requests = []
        self.responses = queue.Queue()
        self.alive = False
        self.closed = False
        self.resets = 0
        self.kwargs = kwargs
        self.instances.append(self)

    def start(self, cancel_event=None):
        self.alive = True
        self.order.append(("start", self.config["id"]))
        while self.hang_start:
            if cancel_event.is_set():
                raise RuntimeError("cancelled")
            time.sleep(0.005)

    def submit(self, request):
        self.requests.append(request)

    def finish(self, actions):
        request = self.requests[-1]
        self.responses.put({"type": "result", "images_released": True, **{key: request[key] for key in ["request_id", "generation", "config_version"]}, "actions": np.asarray(actions).tolist()})

    def poll(self):
        try:
            return self.responses.get_nowait()
        except queue.Empty:
            return None

    def update_config(self, patch, version, cancel_event=None):
        self.config_version = version

    def reset_context(self, cancel_event=None):
        self.resets += 1

    def close(self):
        if not self.closed:
            self.order.append(("close", self.config["id"]))
        self.closed = True
        self.alive = False
        return False


@pytest.fixture
def setup_controller(tmp_path):
    FakeProcess.instances = []
    FakeProcess.order = []
    FakeProcess.hang_start = False
    for identity in ["first", "second"]:
        directory = tmp_path / "policy" / identity
        directory.mkdir(parents=True)
        (directory / "main.py").write_text("\n".join(f"def {name}(*args): pass" for name in ["initialize", "policy", "reset_context", "release"]))
        (directory / "main.py").with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
        (directory / "policy.yaml").write_text(json.dumps({"schema_version": 3, "id": identity,  "execution": {"mode": "sync", "call_every_actions": 2, "control_mode": "qpos"}, "params": {"value": 1}, "hot_reloadable": ["params.value", "execution.mode"]}))
    config = {"schema_version": 3, "frequency_hz": 100, "policy_directory": "policy", "startup_policy": "first", "robot": {"backend": "mock"}, "safety": {"enabled": False, "reference_frequency_hz": 50, "max_delta_per_step": [0.1]*14}, "reset_qpos": [0]*14, "outputs_directory": "outputs", "timeouts": {"initialize_s": 2, "inference_s": 2, "release_s": 1}}
    path = tmp_path / "rc.yaml"
    path.write_text(json.dumps(config))
    robot = MockRobot()
    controller = RobotController(path, robot=robot, process_factory=FakeProcess)
    yield controller, robot, tmp_path
    controller.submit("exit")
    assert controller.done.wait(3)
    assert robot.closed
    FakeProcess.hang_start = False


def ready(controller):
    controller.start()
    until(lambda: controller.status()["policy"] == "first" and controller.status()["state"] == "paused")
    return FakeProcess.instances[-1]


def test_sync_wait_stops_robot_sends_and_exit_remains_responsive(setup_controller):
    controller, robot, _ = setup_controller
    process = ready(controller)
    controller.submit("resume")
    until(lambda: len(process.requests) == 1)
    count = len(robot.commands)
    time.sleep(0.08)
    assert len(robot.commands) == count
    controller.submit("exit")
    assert controller.done.wait(2)
    assert process.closed and controller.exit_code == 0


def test_async_single_request_and_consumption_progress(setup_controller):
    controller, robot, _ = setup_controller
    process = ready(controller)
    controller.submit('set policy.execution.mode "async"')
    until(lambda: controller.status()["config_version"] == 2)
    controller.submit("resume")
    until(lambda: len(process.requests) == 1)
    process.finish(np.ones((12,14)))
    until(lambda: len(process.requests) == 2)
    until(lambda: controller.status()["consumed"] >= 12)
    assert len(process.requests) == 2
    assert controller.status()["queue_remaining"] == 0


def test_stale_result_after_pause_is_discarded_and_resume_resets(setup_controller):
    controller, robot, _ = setup_controller
    process = ready(controller)
    controller.submit("resume")
    until(lambda: len(process.requests) == 1)
    controller.submit("pause")
    until(lambda: controller.status()["state"] == "paused")
    process.finish(np.ones((5,14)))
    until(lambda: not controller.status()["in_flight"])
    assert controller.status()["queue_remaining"] == 0
    controller.submit("resume")
    until(lambda: len(process.requests) == 2)
    assert process.resets >= 2


def test_cold_reload_unloads_before_start_and_hot_patch_preserves_worker(setup_controller):
    controller, robot, _ = setup_controller
    process = ready(controller)
    controller.submit("set policy.params.value 9")
    until(lambda: controller.status()["config_version"] == 2)
    assert FakeProcess.instances[-1] is process and not process.closed
    controller.submit("set policy.execution.call_every_actions 3")
    until(lambda: len(FakeProcess.instances) == 2 and controller.status()["state"] == "paused")
    assert process.closed
    assert FakeProcess.order[:3] == [("start", "first"), ("close", "first"), ("start", "first")]


def test_state_save_load_and_fifo_resume_then_load(setup_controller):
    controller, robot, directory = setup_controller
    ready(controller)
    expected = robot.read_state()["qpos"]
    controller.submit("save pose")
    path = directory / "outputs" / "state_saves" / "pose.json"
    until(path.exists)
    controller.submit("resume")
    operation = controller.submit("load pose")
    until(lambda: controller.get_operation(operation)["status"] == "succeeded")
    assert controller.status().get("hold_name") == "pose"
    assert controller.status()["state"] == "paused"
    assert read_config(path)["qpos"] == expected


def test_rc_cold_parameter_is_rejected_without_change(setup_controller):
    controller, robot, _ = setup_controller
    ready(controller)
    controller.submit("set rc.frequency_hz 5")
    until(lambda: not controller.events.empty())
    time.sleep(0.03)
    assert controller.config["frequency_hz"] == 100


def test_exit_cancels_initialize_and_repeated_exit_is_idempotent(setup_controller):
    controller, robot, _ = setup_controller
    FakeProcess.hang_start = True
    controller.start()
    until(lambda: FakeProcess.instances and FakeProcess.instances[-1].alive)
    controller.submit("exit")
    controller.submit("exit")
    assert controller.done.wait(2)
    assert all(item.closed for item in FakeProcess.instances)
    assert FakeProcess.order.count(("close", "first")) == 1


def test_exit_during_reset_stops_movement_and_closes(setup_controller):
    controller, robot, _ = setup_controller
    ready(controller)
    controller.submit("reset")
    controller.submit("exit")
    assert controller.done.wait(2)
    assert robot.closed


def test_pause_wins_over_already_queued_resume_ack(setup_controller):
    controller, robot, _ = setup_controller
    process = ready(controller)
    entered, finish = threading.Event(), threading.Event()
    def reset(cancel_event=None):
        entered.set()
        assert finish.wait(2)
    process.reset_context = reset
    controller.submit("resume")
    assert entered.wait(2)
    with controller._lock:
        finish.set()
        until(lambda: not controller._results.empty())
        controller.submit("pause")
    until(lambda: controller.status()["state"] == "paused")
    time.sleep(0.05)
    assert controller.status()["state"] == "paused"
    assert process.requests == []


def test_failed_close_does_not_recycle_reader_lease(setup_controller):
    controller, robot, _ = setup_controller
    process = ready(controller)
    class Lease:
        released = 0
        def release(self):
            self.released += 1
    lease = Lease()
    with controller._lock:
        controller._inflight = {"worker": process, "lease": lease}
        close = process.close
        process.alive = False
        def fail():
            raise RuntimeError("descendant remains")
        process.close = fail
        with pytest.raises(RuntimeError):
            controller._close_worker(process)
        controller._clear_finished_lease()
        assert lease.released == 0
        process.close = close
        controller._close_worker(process)
        controller._clear_finished_lease()
        assert lease.released == 1


def test_delayed_command_cannot_burst_slew_steps(setup_controller):
    controller, robot, _ = setup_controller
    ready(controller)
    times = []
    execute, command = robot.execute, controller._command
    def record(action):
        times.append(time.monotonic())
        execute(action)
    def delayed(line):
        if line == "save slow":
            time.sleep(0.035)
        command(line)
    robot.execute = record
    controller._command = delayed
    controller.submit("save slow")
    until(lambda: len(times) >= 8)
    assert min(np.diff(times)) >= 0.008


def test_stale_feedback_stops_reset_and_holds_last_command(setup_controller):
    controller, robot, _ = setup_controller
    ready(controller)
    old = robot.read_state()["qpos"]
    def stale():
        raise RuntimeError("stale arm feedback")
    robot.read_state = stale
    before = len(robot.commands)
    controller.submit("reset")
    until(lambda: controller.status()["state"] == "fault")
    time.sleep(0.025)
    assert all(np.array_equal(action, old) for action in robot.commands[before:])


def test_log_write_failure_does_not_prevent_exit(setup_controller):
    controller, robot, _ = setup_controller
    ready(controller)
    class BrokenLog:
        def write(self, text):
            raise OSError("disk full")
        def close(self):
            pass
    with controller._log_lock:
        controller._log_stream.close()
        controller._log_stream = BrokenLog()
    controller.submit("status")
    until(lambda: controller.exit_code == 1)
    controller.submit("exit")
    assert controller.done.wait(2)
    assert controller.exit_code == 1 and robot.closed


def test_failed_publish_keeps_last_successful_target(setup_controller):
    controller, robot, _ = setup_controller
    ready(controller)
    before = np.asarray(robot.read_state()["qpos"])
    execute = robot.execute
    def fail_new(action):
        if not np.array_equal(action, before):
            raise RuntimeError("publish failed")
        execute(action)
    robot.execute = fail_new
    controller.submit("reset")
    until(lambda: controller.status()["state"] == "fault")
    np.testing.assert_array_equal(controller._hold, before)
    np.testing.assert_array_equal(controller.limiter.previous, before)


def test_start_log_failure_cleans_without_waiting_for_unstarted_loop(setup_controller, monkeypatch):
    from pathlib import Path
    controller, robot, _ = setup_controller
    original = Path.mkdir
    def fail_logs(path, *args, **kwargs):
        if path == controller.outputs / "logs":
            raise OSError("log directory unavailable")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", fail_logs)
    images = controller._pool
    with pytest.raises(OSError):
        controller.start()
    assert controller.done.is_set() and controller.exit_code == 1
    assert robot.closed and images._closed and images._bytes == 0
