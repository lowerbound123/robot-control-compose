import math

import numpy as np
import pytest

from test_controller import setup_controller as setup_controller, ready


def test_tui_settings_defaults_and_independent_arrays():
    from robot_control.tui_config import tui_settings
    first, second = tui_settings(), tui_settings()
    assert first["camera"]["protocol"] == "kitty"
    assert first["monitor"]["qpos_thres"] == [0.02] * 6 + [0.12] + [0.02] * 6 + [0.12]
    assert first["monitor"]["epos_thres"] == ([0.005] * 3 + [0.05] * 3 + [0.12]) * 2
    first["monitor"]["qpos_thres"][0] = 9
    assert second["monitor"]["qpos_thres"][0] == .02
    assert tui_settings({"camera": {"protocol": "sixel"}})["camera"]["protocol"] == "sixel"
    assert tui_settings({"monitor": {"epos_thres": [0] * 14}})["monitor"]["epos_thres"] == [0] * 14


@pytest.mark.parametrize("raw", [
    [], {"camera": []}, {"camera": {"protocol": "auto"}}, {"unexpected": 1},
    {"monitor": {"qpos_thres": [1] * 13}},
    {"monitor": {"epos_thres": [1] * 15}},
    {"monitor": {"qpos_thres": [True] * 14}},
    {"monitor": {"epos_thres": [-1] * 14}},
    {"monitor": {"qpos_thres": [float("nan")] * 14}},
    {"monitor": {"qpos_thres": [float("inf")] * 14}},
])
def test_tui_settings_reject_invalid_config(raw):
    from robot_control.tui_config import tui_settings
    with pytest.raises(ValueError):
        tui_settings(raw)


def test_mock_camera_sequence_is_driven_by_time_not_reads(monkeypatch):
    from robot_control.robots import mock
    now = [100.]
    monkeypatch.setattr(mock.time, "monotonic", lambda: now[0])
    robot = mock.MockRobot()
    robot.connect()
    try:
        first = robot.camera_preview_snapshot()
        second = robot.camera_preview_snapshot()
        assert first["head"]["sequence"] == second["head"]["sequence"] == 1
        assert first["head"]["received"] == second["head"]["received"] == 100.
        now[0] += .1
        third = robot.camera_preview_snapshot()
        assert third["head"]["sequence"] > first["head"]["sequence"]
        assert 100.06 <= third["head"]["received"] <= now[0]
        assert not third["head"]["image"].flags.writeable
    finally:
        robot.close()


def test_monitor_has_no_target_until_success_and_invalidates_failed_publish(setup_controller, monkeypatch):
    controller, robot, _ = setup_controller
    # Keep the background hold publisher outside this deterministic unit test;
    # only the explicit sends below should establish published targets.
    monkeypatch.setattr(controller, '_send_hold', lambda: None)
    ready(controller)
    with controller._lock:
        assert controller.monitor_snapshot()["targets"] == {}
        controller._send_limited(np.ones(14) * .1)
        first = controller.monitor_snapshot()
        assert first["targets"]["left"]["mode"] == "qpos"
        assert first["targets"]["left"]["values"] == pytest.approx([.1] * 7)
        # Re-anchoring the limiter is not a published robot command.
        controller.limiter.reset(np.ones(14) * .7)
        assert controller.monitor_snapshot()["targets"] == first["targets"]
        controller._send_direct({"left": {"mode": "epos", "values": [.2] * 7}}, robot.read_state())
        mixed = controller.monitor_snapshot()
        assert mixed["targets"]["left"]["mode"] == "epos"
        assert mixed["targets"]["right"]["mode"] == "qpos"
        def fail(*args, **kwargs):
            raise RuntimeError("publication failed")
        monkeypatch.setattr(robot, "execute_arms", fail)
        with pytest.raises(RuntimeError):
            controller._send_direct({"left": {"mode": "epos", "values": [.3] * 7}}, robot.read_state())
        assert set(controller.monitor_snapshot()["targets"]) == {"right"}
        monkeypatch.setattr(robot, "read_state", fail)
        stale = controller.monitor_snapshot()
        assert stale["qpos"] is None and stale["epos"] is None
        assert stale["error"]


def test_monitor_highlights_only_active_mode_components_with_shortest_rpy_difference():
    from robot_control.telemetry import monitor_cells
    from robot_control.tui_config import tui_settings
    settings = tui_settings()["monitor"]
    # Binary-exact thresholds make equality independently testable.
    settings["qpos_thres"] = [.125] * 14
    snapshot = {
        "qpos": [.125, .25, 0, 0, 0, 0, 0] + [99.] * 7,
        "epos": [99.] * 7 + [.006, 0, 0, -math.pi + .01, 0, 0, .13],
        "targets": {
            "left": {"mode": "qpos", "values": [0.] * 7},
            "right": {"mode": "epos", "values": [0, 0, 0, math.pi - .01, 0, 0, 0]},
        }, "error": None,
    }
    cells = monitor_cells(snapshot, settings)
    assert cells["qpos"]["red"] == [False, True] + [False] * 12
    assert cells["epos"]["red"] == [False] * 7 + [True, False, False, False, False, False, True]
    assert cells["qpos"]["text"][0] == "0.1250"
    snapshot["targets"] = {}
    assert not any(monitor_cells(snapshot, settings)["qpos"]["red"])
    snapshot.update(qpos=None, epos=None, error="stale feedback")
    cells = monitor_cells(snapshot, settings)
    assert cells["qpos"]["text"] == ["--"] * 14
    assert not any(cells["epos"]["red"])
