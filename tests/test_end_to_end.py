"""Controller + socket supervisor + worker + policy integration, without Conda/ROS.

Only interpreter selection is injected. Real subprocesses, mmap image leases,
config acknowledgments, policy callbacks and process cleanup remain in use.
"""
from project_templates import template_path, copy_templates

import json
import asyncio
import os
from pathlib import Path
import shutil
import sys
import time

import posix_ipc
import numpy as np
import pytest

from robot_control.controller import RobotController
from host_helpers import LocalPolicyProcess as PolicyProcess
from robot_control.robots import MockRobot


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_POLICY = Path(__file__).parent / "fixtures" / "e2e_policy.py"


def wait_for(predicate, *, timeout=4, detail="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    pytest.fail(f"timed out waiting for {detail}")


class EndToEnd:
    def __init__(self, directory):
        self.directory = directory
        self.events_path = directory / "policy-events.jsonl"
        self.workers = []
        self.prior_workers_alive_at_creation = []
        self.robot = MockRobot({"initial_qpos": [0.0] * 14, "image_size": [8, 12]})
        self.controller = None

    def add_policy(self, identity="first", *, example=None, mode="async", **params):
        folder = self.directory / "policy" / identity
        folder.mkdir(parents=True)
        if example:
            source = ROOT / "policy" / example if example == "hold" else ROOT / "tests/fixtures/policies" / example
            config = json.loads((template_path(source / "policy.json")).read_text())
            config["id"] = identity
            config["execution"]["mode"] = mode
            config["params"].update(params)
            shutil.copy(source / "main.py", folder / "main.py")
        else:
            config = {
                "schema_version": 3, "id": identity,
                "execution": {"mode": mode, "call_every_actions": 2, "control_mode": "qpos"},
                "hot_reloadable": ["params.value", "params.delay_s", "execution.mode"],
                "params": {
                    "events_path": str(self.events_path), "value": 0.02, "chunk_size": 20,
                    "delay_s": 0.03, "initialize_delay_s": 0, "release_delay_s": 0,
                    "spawn_child": False, "detached_child": False, **params,
                },
            }
            shutil.copy(FIXTURE_POLICY, folder / "main.py")
        (folder / "compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
        (folder / "policy.json").write_text(json.dumps(config))
        return folder

    def launch(self, *, startup="first", release_s=0.6, inference_s=3):
        config = {
            "schema_version": 3, "frequency_hz": 100, "policy_directory": "policy",
            "startup_policy": startup,
            "robot": {"backend": "mock"},
            "safety": {"enabled": True, "reference_frequency_hz": 100,
                       "max_delta_per_step": [0.1] * 14},
            "reset_qpos": [0.0] * 14, "outputs_directory": "outputs",
            "timeouts": {"initialize_s": 4, "inference_s": inference_s, "release_s": release_s},
        }
        path = self.directory / "rc.json"
        path.write_text(json.dumps(config))

        def real_process_factory(*args, **kwargs):
            self.prior_workers_alive_at_creation.append([worker.alive for worker in self.workers])
            worker = PolicyProcess(*args, python_command=[sys.executable], **kwargs)
            self.workers.append(worker)
            return worker

        self.controller = RobotController(path, robot=self.robot, process_factory=real_process_factory)
        self.image_pool = self.controller._pool
        self.controller.start()
        return self.controller

    def ready(self, identity="first", count=1):
        return wait_for(
            lambda: len(self.workers) == count and self.controller.status()["policy"] == identity
            and self.controller.status()["state"] == "paused",
            detail=f"{identity} initialized and paused",)

    def events(self, kind=None, *, identity=None, pid=None):
        if not self.events_path.exists():
            return []
        rows = []
        for line in self.events_path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # A polling read may observe the last append before its newline.
                break
            if (kind is None or row["kind"] == kind) and (identity is None or row["id"] == identity) and (pid is None or row["pid"] == pid):
                rows.append(row)
        return rows

    def stop_and_assert_clean(self, *, expected_code=None):
        if self.controller is None:
            return
        self.controller.submit("exit")
        assert self.controller.done.wait(7), self.controller.status()
        self.controller._thread.join(1)
        assert not self.controller._thread.is_alive()
        assert not self.controller._manager.is_alive()
        assert self.robot.closed
        assert self.image_pool._bytes == 0, "leased shared segments survived shutdown"
        assert self.controller._inflight is None
        for worker in self.workers:
            assert not worker.alive
            if worker.pid is not None:
                with pytest.raises(ProcessLookupError):
                    os.kill(worker.pid, 0)
                with pytest.raises(ProcessLookupError):
                    os.killpg(worker.pid, 0)
            assert worker.cleanup_confirmed, "runtime exit is unconfirmed"
        for row in self.events("child"):
            with pytest.raises(ProcessLookupError):
                os.kill(row["child_pid"], 0)
        if expected_code is not None:
            assert self.controller.exit_code == expected_code


@pytest.fixture
def e2e(tmp_path):
    harness = EndToEnd(tmp_path)
    yield harness
    harness.stop_and_assert_clean()


def test_real_hold_example_consumes_absolute_targets(e2e):
    e2e.add_policy(example="hold", mode="sync", chunk_size=6)
    controller = e2e.launch()
    e2e.ready()
    controller.submit("resume")
    wait_for(lambda: controller.status()["consumed"] >= 4, detail="real hold targets consumed")
    assert all(np.array_equal(command, np.zeros(14)) for command in e2e.robot.commands)
    e2e.stop_and_assert_clean(expected_code=0)


def test_real_delayed_hold_sync_blocks_consumption_during_inference(e2e):
    e2e.add_policy(example="delayed_hold", mode="sync", delay_s=0.2, chunk_size=6)
    controller = e2e.launch()
    e2e.ready()
    controller.submit("resume")
    wait_for(lambda: controller.status()["in_flight"], detail="sync inference")
    count = len(e2e.robot.commands)
    consumed = controller.status()["consumed"]
    time.sleep(0.04)
    assert len(e2e.robot.commands) == count
    assert controller.status()["consumed"] == consumed
    wait_for(lambda: controller.status()["consumed"] >= 2)
    e2e.stop_and_assert_clean(expected_code=0)


def test_real_delayed_hold_async_exhaustion_holds_without_false_consumption(e2e):
    e2e.add_policy(example="delayed_hold", delay_s=0.15, chunk_size=4)
    controller = e2e.launch()
    e2e.ready()
    controller.submit("resume")
    wait_for(lambda: controller.status()["consumed"] == 4
             and controller.status()["queue_remaining"] == 0
             and controller.status()["in_flight"], detail="async cache exhausted")
    sent = len(e2e.robot.commands)
    time.sleep(0.04)
    assert controller.status()["consumed"] == 4
    assert len(e2e.robot.commands) > sent
    assert all(np.array_equal(command, np.zeros(14)) for command in e2e.robot.commands)
    e2e.stop_and_assert_clean(expected_code=0)


def test_hot_patch_preserves_real_worker_and_cold_reload_closes_before_new_initialize(e2e):
    e2e.add_policy(delay_s=0.06)
    controller = e2e.launch()
    e2e.ready()
    old = e2e.workers[0]
    old_pid = old.pid
    old_policy_pid = e2e.events("initialize_start", identity="first")[0]["pid"]
    controller.submit("resume")
    wait_for(lambda: e2e.events("inference_start"))
    controller.submit("set policy.params.value 0.04")
    wait_for(lambda: controller.status()["pending_config"] is not None)
    assert controller.status()["config_version"] == 1
    wait_for(lambda: controller.status()["config_version"] == 2)
    updated = wait_for(lambda: [event for event in e2e.events("inference_start") if event["value"] == 0.04])
    assert len(e2e.workers) == 1 and old.pid == old_pid
    assert updated[-1]["same_config"] and updated[-1]["readonly"]
    assert updated[-1]["cameras"] == ["head", "left_wrist", "right_wrist"]
    wait_for(lambda: np.allclose(e2e.robot.read_state()["qpos"], [0.04] * 14),
             detail="hot config output reaches robot")
    before_reload = e2e.robot.read_state()["qpos"]
    controller.submit("set policy.execution.call_every_actions 5")
    e2e.ready(count=2)
    assert controller.status()["config_version"] == 3
    assert e2e.prior_workers_alive_at_creation[1] == [False]
    assert not old.alive and e2e.workers[1].pid != old_pid
    old_end = e2e.events("release_done", pid=old_policy_pid)[0]
    new_start = e2e.events("initialize_start", identity="first")[-1]
    assert old_end["time_ns"] < new_start["time_ns"]
    np.testing.assert_allclose(e2e.robot.read_state()["qpos"], before_reload, atol=1e-7)
    e2e.stop_and_assert_clean(expected_code=0)


def test_pause_load_discards_old_result_and_resume_observes_loaded_qpos(e2e):
    e2e.add_policy(delay_s=0.2)
    controller = e2e.launch()
    e2e.ready()
    controller.submit("resume")
    wait_for(lambda: e2e.events("inference_start"))
    controller.submit("pause")
    wait_for(lambda: controller.status()["state"] == "paused")
    generation = controller.status()["generation"]
    target = e2e.directory / "loaded.json"
    target.write_text(json.dumps({"name": "loaded", "qpos": [0.2] * 14, "old_format_extra": True}))
    controller.submit(f'load "{target}"')
    wait_for(lambda: controller.status()["hold_name"] == "loaded")
    assert controller.status()["state"] == "paused"
    assert controller.status()["generation"] > generation
    wait_for(lambda: not controller.status()["in_flight"])
    assert controller.status()["queue_remaining"] == 0
    np.testing.assert_allclose(e2e.robot.read_state()["qpos"], [0.2] * 14)
    controller.submit("resume")
    observed = wait_for(lambda: [row for row in e2e.events("inference_start") if np.isclose(row["qpos"][0], 0.2)])
    assert observed[-1]["same_config"]
    assert len(e2e.events("reset_context")) >= 2
    controller.submit("save resumed")
    saved = e2e.directory / "outputs" / "state_saves" / "resumed.json"
    wait_for(saved.exists)
    wait_for(lambda: controller.status()["state"] == "paused")
    assert set(json.loads(saved.read_text())) == {"name", "saved_at", "qpos", "source", "run_id"}
    e2e.stop_and_assert_clean(expected_code=0)


def test_exit_in_inference_keeps_image_lease_until_worker_finishes(e2e):
    e2e.add_policy(delay_s=0.2)
    controller = e2e.launch()
    e2e.ready()
    controller.submit("resume")
    wait_for(lambda: e2e.events("inference_start"))
    images = [value["shm_name"] for value in controller._inflight["lease"].descriptors.values()]
    controller.submit("exit")
    time.sleep(0.025)
    assert not controller.done.is_set()
    assert e2e.workers[0].alive
    assert all(segment_exists(name) for name in images)
    e2e.stop_and_assert_clean(expected_code=0)
    assert all(not segment_exists(name) for name in images)
    assert len(e2e.events("release_done")) == 1


def test_exit_during_real_initialize_cancels_and_cleans_process(e2e):
    e2e.add_policy(initialize_delay_s=2)
    controller = e2e.launch(release_s=0.08)
    wait_for(lambda: e2e.events("initialize_start"))
    assert controller.status()["state"] == "initializing"
    started = time.monotonic()
    controller.submit("exit")
    e2e.stop_and_assert_clean(expected_code=1)
    assert time.monotonic() - started < 2
    assert not e2e.events("initialize_done")
    # A blocked initialization cannot be safely released concurrently. The host
    # proves process-tree exit after the bounded, forced group stop.
    assert not e2e.events("release_done")
    assert e2e.workers[0].cleanup_confirmed


@pytest.mark.parametrize("phase", ["old_release", "new_initialize"])
def test_exit_during_switch_never_orphans_either_real_worker(e2e, phase):
    e2e.add_policy(release_delay_s=0.18)
    e2e.add_policy("second", initialize_delay_s=2)
    controller = e2e.launch(release_s=0.5 if phase == "old_release" else 0.08)
    e2e.ready()
    controller.submit("switch second")
    if phase == "old_release":
        wait_for(lambda: e2e.events("release_start", identity="first"))
    else:
        wait_for(lambda: e2e.events("initialize_start", identity="second"))
    controller.submit("exit")
    e2e.stop_and_assert_clean(expected_code=0 if phase == "old_release" else 1)
    if phase == "old_release":
        assert len(e2e.workers) == 1
        assert not e2e.events("initialize_start", identity="second")
    else:
        assert len(e2e.workers) == 2
        assert not e2e.events("initialize_done", identity="second")


@pytest.mark.parametrize("detached", [False, True])
def test_forced_release_reports_failure_and_reclaims_children_sockets_and_images(e2e, detached):
    e2e.add_policy(release_delay_s=0.25, spawn_child=True, detached_child=detached)
    controller = e2e.launch(release_s=0.05)
    e2e.ready()
    child = e2e.events("child")[0]["child_pid"]
    os.kill(child, 0)
    controller.submit("resume")
    wait_for(lambda: e2e.events("inference_start"))
    assert e2e.image_pool._bytes > 0
    e2e.stop_and_assert_clean(expected_code=1)
    assert len(e2e.events("release_start")) == 1


@pytest.mark.asyncio
async def test_real_tui_accepts_exit_during_worker_initialization(e2e):
    from robot_control.tui import RobotControlApp, CommandInput
    folder = e2e.add_policy(initialize_delay_s=10)
    path = folder / "main.py"
    path.write_text(path.read_text().replace('record("initialize_start")', 'record("initialize_start")\n    for i in range(500): print(f"initialize log {i}", flush=True)'))
    controller = e2e.launch(release_s=0.08)
    app = RobotControlApp(controller, start_controller=False)
    async with app.run_test(size=(100, 35)) as pilot:
        await asyncio.to_thread(wait_for, lambda: e2e.events("initialize_start"))
        entry = app.query_one(CommandInput)
        entry.value = "status"
        await pilot.press("enter")
        assert "status" in entry.history and entry.value == ""
        entry.value = "exit"
        await pilot.press("enter")
        assert await asyncio.to_thread(controller.done.wait, 7)
        assert "exit" in entry.history
        # Let the TUI drain its events and exit before run_test removes widgets.
        await asyncio.to_thread(wait_for, lambda: not app.is_running, detail="TUI exit")
    e2e.stop_and_assert_clean()


def test_old_inference_error_cannot_cancel_requested_policy_switch(e2e):
    e2e.add_policy(delay_s=0.15, raise_inference=True)
    e2e.add_policy("second")
    controller = e2e.launch()
    e2e.ready()
    controller.submit("resume")
    wait_for(lambda: e2e.events("inference_start", identity="first"))
    controller.submit("switch second")
    e2e.ready(identity="second", count=2)
    assert controller.status()["state"] == "paused"
    assert not e2e.workers[0].alive
    assert controller._inflight is None
    e2e.stop_and_assert_clean(expected_code=0)


def segment_exists(name):
    try: segment = posix_ipc.SharedMemory(name, read_only=True)
    except posix_ipc.ExistentialError: return False
    segment.close_fd()
    return True
