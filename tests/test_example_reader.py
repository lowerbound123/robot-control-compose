"""The example must not release a remote reader's pin on an unrelated ACK."""
import json
from multiprocessing import get_context
from pathlib import Path

import numpy as np
import pytest
from rc_policy_sdk.images import ImagePool
from robot_control.external import ExternalResources

from config_helpers import read_config
from example_reader import serve
from host_helpers import LocalPolicyProcess
from project_templates import copy_templates
from test_host_transport import wait_for


@pytest.mark.parametrize("wrong_ack", [False, True])
def test_external_process_reads_frames_and_rc_requires_matching_release(tmp_path, wrong_ack):
    ctx = get_context("spawn")
    rx, tx = ctx.Pipe(duplex=False)
    peer = ctx.Process(target=serve, args=(("127.0.0.1", 0), tx.send), kwargs={"wrong_ack": wrong_ack})
    peer.start()
    tx.close()
    try:
        assert rx.poll(5)
        endpoint = f"http://127.0.0.1:{rx.recv()}"
        folder = copy_templates(Path(__file__).resolve().parents[1] / "policy/example", tmp_path / "policy/example")
        config = read_config(folder / "policy.yaml")
        config["params"]["reader_url"] = endpoint
        pool = ImagePool(cameras=("head",))
        pool.publish("head", np.full((3, 4, 3), 17, np.uint8), 1.)
        lease = pool.acquire()
        worker = LocalPolicyProcess(folder, config, init_timeout_s=6, release_timeout_s=2)
        guard = ExternalResources(pool, tmp_path / "external.json")
        worker.resources = guard
        try:
            worker.start()
            worker.submit(dict(request_id=1, generation=1, config_version=1, images=lease.descriptors,
                               state={"qpos": [0.] * 14}, action_chunk=[]))
            reply = wait_for(worker.poll)
            assert reply["images_released"]  # local worker mappings; independent of the remote pin
            records = json.loads(guard.recovery_path.read_text())["remote_models"]
            assert len(records) == 1 and records[0]["generation"] == 1
            if wrong_ack:
                assert reply["type"] == "error" and "未确认" in reply["error"]
                assert list(records[0]["requests"].values()) == [lease.descriptors]
            else:
                assert reply["type"] == "result" and records[0]["requests"] == {}
        finally:
            assert not worker.close() and worker.cleanup_confirmed
            lease.release()
            pool.close()
            try:
                if wrong_ack and guard.recovery_path.exists():
                    assert pool._bytes > 0  # local exit alone did not reclaim this reader's frame
            finally:
                guard.close_worker(worker, timeout=4)
                guard.close()
            assert pool._bytes == 0 and not guard.recovery_path.exists()
    finally:
        rx.close()
        peer.terminate()
        peer.join(5)
        assert not peer.is_alive()
