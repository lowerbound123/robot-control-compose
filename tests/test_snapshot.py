from config_helpers import read_config
import asyncio
from concurrent.futures import Future
from io import StringIO
import json
import threading

import numpy as np
from PIL import Image
import pytest

from robot_control.terminal_commands import TerminalCommandRouter
from test_controller import setup_controller as setup_controller, ready, until


@pytest.fixture
def snapshot_router(setup_controller, monkeypatch):
    controller, robot, directory = setup_controller
    ready(controller)
    working = directory / "working"
    working.mkdir()
    monkeypatch.chdir(working)
    router = TerminalCommandRouter(controller)
    yield router, controller, robot, working
    router.close()


def test_snapshot_preserves_three_original_images_and_complete_state(snapshot_router, monkeypatch):
    router, controller, robot, working = snapshot_router
    read_state = robot.read_state

    def complete_state():
        return {**read_state(), "raw": {"left": {"diagnostic": {"temperatures": [21, 22]}}},
                "extra_feedback": {"enabled": True}}

    monkeypatch.setattr(robot, "read_state", complete_state)
    controller.submit("resume")
    until(lambda: controller.status()["state"] == "running")
    worker = controller._worker
    until(lambda: worker.requests)
    worker.finish([[0.1] * 14])
    until(lambda: controller.monitor_snapshot()["targets"])
    generation = controller.status()["generation"]
    images = robot.read_images()
    future = router.submit("snapshot")
    assert isinstance(future, Future)
    folder = future.result(timeout=3)
    until(lambda: not router.pending)
    assert folder.parent == working / "snapshot"
    assert not (controller.root / "snapshot").exists()
    assert {path.name for path in folder.iterdir()} == {
        "head.png", "left_wrist.png", "right_wrist.png", "state.json"}
    payload = read_config(folder / "state.json")
    assert payload["state"]["raw"] == {"left": {"diagnostic": {"temperatures": [21, 22]}}}
    assert payload["state"]["extra_feedback"] == {"enabled": True}
    assert {"qpos", "qvel", "effort", "eef", "timestamp", "sequences"} <= payload["state"].keys()
    assert payload["control"]["state"] == "running"
    assert payload["run_id"] == controller.run_id and payload["targets"]
    for camera, (pixels, _) in images.items():
        with Image.open(folder / f"{camera}.png") as saved:
            np.testing.assert_array_equal(np.asarray(saved), pixels)
        assert payload["images"][camera]["file"] == f"{camera}.png"
        assert payload["images"][camera]["timestamp"] > 0
    assert controller.status()["state"] == "running"
    assert controller.status()["generation"] == generation
    first_json = (folder / "state.json").read_bytes()
    second = router.submit("snapshot").result(timeout=3)
    until(lambda: not router.pending)
    assert second != folder and (folder / "state.json").read_bytes() == first_json


@pytest.mark.parametrize("unavailable", ["camera", "state"])
def test_snapshot_rejects_incomplete_feedback_without_partial_folder(snapshot_router, monkeypatch, unavailable):
    router, _, robot, working = snapshot_router
    if unavailable == "camera":
        images = robot.read_images()
        monkeypatch.setattr(robot, "read_images", lambda: {"head": images["head"]})
    else:
        def stale():
            raise RuntimeError("stale left arm feedback")
        monkeypatch.setattr(robot, "read_state", stale)
    with pytest.raises(RuntimeError, match="camera|feedback"):
        router.submit("snapshot")
    assert not (working / "snapshot").exists()


def test_snapshot_writes_in_background_and_finishes_after_controller_exit(snapshot_router, monkeypatch):
    router, controller, _, working = snapshot_router
    entered, release = threading.Event(), threading.Event()
    save = Image.Image.save
    writer_threads = []

    def slow_save(image, *args, **kwargs):
        writer_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return save(image, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", slow_save)
    try:
        future = router.submit("snapshot")
        assert entered.wait(1) and router.pending
        with pytest.raises(RuntimeError, match="snapshot"):
            router.submit("snapshot")
        identity = router.submit("status")
        assert controller.get_operation(identity)["status"] == "succeeded"
        controller.submit("exit")
        assert controller.done.wait(3)
        router.close()
        assert router.pending
        release.set()
        folder = future.result(timeout=3)
        until(lambda: not router.pending)
        assert folder.parent == working / "snapshot" and (folder / "state.json").is_file()
        assert writer_threads and all(identity != threading.get_ident() for identity in writer_threads)
    finally:
        release.set()


def test_snapshot_write_error_cleans_only_its_own_partial_folder(snapshot_router, monkeypatch):
    router, _, _, working = snapshot_router
    root = working / "snapshot"
    root.mkdir()
    previous = root / "previous.txt"
    previous.write_text("keep")
    original = Image.Image.save
    calls = []

    def fail_second(image, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise OSError("disk full")
        return original(image, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", fail_second)
    with pytest.raises(OSError, match="disk full"):
        router.submit("snapshot").result(timeout=3)
    until(lambda: not router.pending)
    assert list(root.iterdir()) == [previous] and previous.read_text() == "keep"


def test_snapshot_help_and_argument_validation(snapshot_router):
    router, controller, _, _ = snapshot_router
    router.submit("help snapshot")
    assert any("snapshot/" in event["message"] for event in controller.read_events()["events"])
    with pytest.raises(ValueError, match="snapshot"):
        router.submit("snapshot extra")


def test_headless_snapshot_completes_before_exit_returns(snapshot_router):
    from robot_control.cli import run_headless
    router, controller, _, working = snapshot_router
    output = StringIO()
    run_headless(controller, stdin=StringIO("snapshot\nexit\n"), stdout=output, router=router)
    assert len(list((working / "snapshot").glob("*/state.json"))) == 1
    assert "snapshot 已保存" in output.getvalue()


@pytest.mark.asyncio
async def test_tui_snapshot_completion_and_command_without_camera_panel(snapshot_router):
    from robot_control.tui import CommandInput, RobotControlApp
    router, controller, _, working = snapshot_router
    app = RobotControlApp(controller, start_controller=False, router=router)
    async with app.run_test() as pilot:
        command = app.query_one(CommandInput)
        command.value = "snap"
        await pilot.press("tab")
        assert command.value == "snapshot "
        await pilot.press("enter")
        await asyncio.to_thread(until, lambda: not router.pending)
        assert len(list((working / "snapshot").glob("*/state.json"))) == 1
        app.action_safe_exit()
        await asyncio.to_thread(until, lambda: not app.is_running)
