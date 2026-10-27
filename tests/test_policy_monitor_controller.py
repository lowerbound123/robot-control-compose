import sys
import time

import pytest

from host_helpers import LocalPolicyProcess as PolicyProcess
from test_controller import setup_controller
from test_policy_monitor_worker import SOURCE, wait_until


def test_sampling_ignores_control_frequency_and_tracks_lifecycle(setup_controller):
    controller, robot, root = setup_controller
    for name in ("first", "second"):
        record = controller.registry.get(name)
        (record.directory / "main.py").write_text(SOURCE)
        (record.directory / "main.py").with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
        record.config["params"]["root"] = str(record.directory)
        controller.registry.save(name)
    controller.config["frequency_hz"] = 1
    controller.process_factory = lambda *args, **kw: PolicyProcess(*args, python_command=[sys.executable], **kw)
    controller.start()
    wait_until(lambda: controller.status()["state"] == "paused" and controller._worker is not None, 6)
    hub = controller.policy_monitors
    hub.set_enabled("first", True)
    wait_until(lambda: hub.snapshot().get("first", {}).get("values"))
    initial = hub.snapshot()["first"]["values"]["count"]
    time.sleep(.45)
    assert hub.snapshot()["first"]["values"]["count"] >= initial + 3
    assert controller.status()["state"] == "paused"
    with pytest.raises(PermissionError, match="local-only"):
        controller.submit("monitor-first", origin="mcp")
    take = controller.submit("take")
    wait_until(lambda: controller.get_operation(take)["status"] == "succeeded", 5)
    initial = hub.snapshot()["first"]["values"]["count"]
    wait_until(lambda: hub.snapshot()["first"]["values"]["count"] > initial)
    returned = controller.submit("return")
    wait_until(lambda: controller.get_operation(returned)["status"] == "succeeded", 5)
    controller.submit("pause")
    wait_until(lambda: controller.status()["state"] == "paused")
    old = controller._worker
    changed = controller.submit("switch first")
    wait_until(lambda: controller.get_operation(changed)["status"] == "succeeded", 6)
    assert controller._worker is not old
    wait_until(lambda: hub.snapshot().get("first", {}).get("values"))
    assert list(hub.snapshot()) == ["first"]
    changed = controller.submit("switch second")
    wait_until(lambda: controller.get_operation(changed)["status"] == "succeeded", 6)
    wait_until(lambda: not hub.snapshot())
    controller.submit("exit")
    assert controller.done.wait(5)
    wait_until(lambda: not hub.thread.is_alive())
