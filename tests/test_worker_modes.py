import sys
import time
import numpy as np
import pytest

from host_helpers import LocalPolicyProcess as PolicyProcess
from rc_policy_sdk.images import ImagePool


@pytest.mark.parametrize("mode", ["epos", "mixed"])
def test_real_worker_mixed_output_and_remaining_input(tmp_path, mode):
    directory = tmp_path / "policy"
    directory.mkdir()
    (directory / "main.py").write_text('''
def initialize(config): return None
def reset_context(context): pass
def release(context): pass
def policy(context, state, action_chunk, images, config):
    if config["execution"]["control_mode"] == "epos":
        assert action_chunk == []
        return [state["eef"]]
    assert action_chunk == {"actions": [], "modes": []}
    return {"actions": [state["qpos"], state["eef"]], "modes": ["qpos", "epos"]}
''')
    config = {"id": "mixed", "policy_host": "test", "params": {}, "execution": {"control_mode": mode}}
    worker = PolicyProcess(directory, config, python_command=[sys.executable])
    pool = ImagePool(tmp_path / "frames", cameras=("head",))
    pool.publish("head", np.zeros((2, 2, 3), np.uint8), 1.)
    lease = pool.acquire()
    try:
        worker.start()
        worker.submit({"request_id": 1, "generation": 0, "config_version": 1,
                       "state": {"qpos": [0] * 14, "eef": [.5] * 14},
                       "action_chunk": {"actions": [], "modes": []} if mode == "mixed" else [], "images": lease.descriptors})
        deadline = time.monotonic() + 3
        result = None
        while result is None and time.monotonic() < deadline:
            result = worker.poll()
            time.sleep(.01)
        assert result["type"] == "result", result
        assert result["modes"] == (["qpos", "epos"] if mode == "mixed" else ["epos"])
        assert result["actions"] == ([[0.] * 14, [.5] * 14] if mode == "mixed" else [[.5] * 14])
    finally:
        worker.close()
        lease.release()
        pool.close()
