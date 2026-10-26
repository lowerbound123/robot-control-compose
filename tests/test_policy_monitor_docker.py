"""Actual shared-memory/UDS tests; no model weights or provider API calls."""
import os
from pathlib import Path
import sys

import numpy as np
import pytest

from rc_policy_sdk.images import ImagePool
from robot_control.chain_process import ChainProcess
from robot_control.config import PolicyRegistry
from robot_control.policy_monitors import PolicyMonitors
from robot_control.process import PolicyProcess
from test_docker_integration import HOSTS, wait

pytestmark = pytest.mark.skipif(os.environ.get("RC_MONITOR_DOCKER_TEST") != "1", reason="requires monitor Compose test override")
ROOT = Path("/workspace/policy")


@pytest.mark.parametrize("split", [False, True])
def test_monitors_in_reused_chain_same_and_cross_python_hosts(split):
    assert sys.version_info[:2] == (3, 10)
    registry = PolicyRegistry(ROOT)
    if split:
        registry.commit("B", {**registry.get("B").config, "policy_host":"second"})
    record = registry.propose_chain(["A", "B", "A"])
    chain = ChainProcess(record, policy_hosts=HOSTS, init_timeout_s=10, inference_timeout_s=5, release_timeout_s=1)
    monitors = PolicyMonitors(lambda: (chain.monitor_members(), False))
    pool = ImagePool()
    for name in ("head", "left_wrist", "right_wrist"):
        pool.publish(name, np.ones((2, 2, 3), np.uint8), 1.)
    lease = pool.acquire()
    try:
        chain.start()
        assert list(chain.monitor_members()) == ["A", "B"]
        monitors.set_enabled("A", True)
        monitors.set_enabled("B", True)
        wait(lambda: all(row["values"] for row in monitors.snapshot().values()))
        before = monitors.snapshot()
        assert before["A"]["values"]["python"].startswith("3.14.")
        assert before["B"]["values"]["python"].startswith("3.11." if split else "3.14.")
        a, b = chain.monitor_members().values()
        assert (a.runtime_id != b.runtime_id) == split
        chain.submit(dict(request_id=1, generation=1, config_version=record.version,
                          state={"qpos":[0]*14}, action_chunk=[], images=lease.descriptors))
        wait(lambda: monitors.snapshot()["A"]["updated_at"] > before["A"]["updated_at"])
        assert chain.poll() is None  # A sleeps while the monitor thread keeps responding
        assert wait(chain.poll)["images_released"] is True
        wait(lambda: monitors.snapshot()["A"]["values"]["calls"] == 2)
        assert monitors.snapshot()["B"]["values"]["calls"] == 1
        chain.reset_context()
        monitors.invalidate()
        wait(lambda: monitors.snapshot()["A"]["values"] is not None)
        assert monitors.snapshot()["A"]["values"]["calls"] == 0
    finally:
        monitors.close()
        chain.close()
        lease.release()
        pool.close()
    assert chain.cleanup_confirmed


def test_monitor_reply_does_not_release_image_slots():
    registry = PolicyRegistry(ROOT)
    cfg = registry.get("A").config
    worker = PolicyProcess(ROOT/"A", cfg, policy_hosts=HOSTS, init_timeout_s=10, inference_timeout_s=3, release_timeout_s=1)
    pool = ImagePool()
    leases = []
    try:
        for camera in ("head", "left_wrist", "right_wrist"):
            pool.publish(camera, np.ones((2, 2, 3), np.uint8), 1.)
        leases.append(pool.acquire())
        worker.start()
        worker.submit(dict(request_id=1, generation=1, config_version=1, state={}, action_chunk=[], images=leases[0].descriptors))
        assert worker.submit_monitor(1)
        reply = wait(worker.poll_monitor)
        assert reply["type"] == "monitor_result" and "images_released" not in reply
        for value in (2, 3):
            assert pool.publish("head", np.full((2, 2, 3), value, np.uint8), float(value))
            leases.append(pool.acquire())
        assert not pool.publish("head", np.zeros((2, 2, 3), np.uint8), 4.)
        assert wait(worker.poll)["images_released"] is True
        leases[0].release()
        assert pool.publish("head", np.zeros((3, 3, 3), np.uint8), 5.)
    finally:
        worker.close()
        for lease in leases: lease.release()
        pool.close()
