"""Shipped example through actual SDK workers, shared images and native chains."""
from config_helpers import read_config
import copy
import json
from pathlib import Path
import sys

import numpy as np
import pytest

from rc_policy_sdk.images import ImagePool
from robot_control.chain_process import ChainProcess
from robot_control.config import PolicyRegistry
from host_helpers import LocalPolicyProcess
from project_templates import copy_templates
from test_policy_monitor_worker import wait_until

ROOT = Path(__file__).resolve().parents[1]


def worker_factory(*args, **kwargs):
    return LocalPolicyProcess(*args, python_command=[sys.executable], **kwargs)


def command_result(worker):
    def completed():
        message = worker.poll_command()
        return message if message and message["type"] != "command_started" else None
    result = wait_until(completed, 5)
    assert result["type"] == "command_result", result
    return result


@pytest.fixture
def image_readers():
    return []


@pytest.fixture
def example_worker(tmp_path, frame_request, image_readers):
    directory = copy_templates(ROOT / "policy" / "example", tmp_path / "policy" / "example")
    config = read_config(directory / "policy.yaml")
    worker = worker_factory(directory, config, init_timeout_s=10, inference_timeout_s=5, release_timeout_s=2)
    image_readers.append(worker)
    try:
        worker.start()
        yield worker, config
    finally:
        assert not worker.close()


@pytest.fixture
def frame_request(tmp_path, image_readers):
    pool = ImagePool(tmp_path / "frames", cameras=("head", "left", "right"))
    for name, pixel in (("head", 7), ("left", 18), ("right", 31)):
        pool.publish(name, np.full((3, 4, 3), pixel, np.uint8), 1.25)
    lease = pool.acquire()
    try:
        yield {
            "request_id": 1, "generation": 2, "config_version": 1,
            "state": {"qpos": [0.25] * 14}, "action_chunk": [], "images": lease.descriptors,
            "runtime": {"observation_id": "example-test", "consumed_actions": 7, "control_epoch": 2},
        }
    finally:
        try:
            assert all(reader.cleanup_confirmed for reader in image_readers), "reader must exit before image teardown"
        finally:
            # Even if the test fails, retain the lease until reader exit is proven.
            for reader in image_readers:
                reader.close()
            assert all(reader.cleanup_confirmed for reader in image_readers), "image readers still unconfirmed"
            lease.release()
            pool.close()
    assert pool._bytes == 0


def test_real_worker_callbacks_commands_and_monitor(example_worker, frame_request):
    worker, _ = example_worker
    assert worker.supports_observe and worker.supports_monitor and worker.supports_commands
    worker.submit(frame_request)
    assert worker.observe({**frame_request, "request_id": 2})
    worker.submit_command("snapshot", "example", [])
    assert worker.submit_monitor(0)
    result = wait_until(worker.poll, 5)
    observed = wait_until(worker.poll_observation, 5)
    monitored = wait_until(worker.poll_monitor, 5)
    assert result["type"] == "result" and result["actions"] == [[0.25] * 14]
    assert result["images_released"] is True and not result.get("control_requests")
    assert observed["type"] == "observed" and observed["images_released"] is True
    assert monitored["type"] == "monitor_result" and "images_released" not in monitored
    assert command_result(worker)["result"]["message"] == "example"
    worker.submit_command("after", "example", [])
    info = command_result(worker)["result"]
    assert info["calls"] == info["observations"] == 1
    assert info["consumed_actions"] == 7 and info["image_count"] == 3
    images = json.loads(info["images"])
    assert all(image["readonly"] for image in images.values())
    assert [images[name]["sample"] for name in ("head", "left", "right")] == ["7", "18", "31"]
    actions = np.arange(28, dtype=np.float32).reshape(2, 14).tolist()
    worker.submit({**frame_request, "request_id": 3, "action_chunk": actions})
    assert wait_until(worker.poll, 5)["actions"] == actions
    worker.reset_context()
    worker.submit_command("reset", "example", [])
    info = command_result(worker)["result"]
    assert info["calls"] == info["observations"] == 0


def test_hot_update_reaches_original_context_and_invalid_patch_is_atomic(example_worker, frame_request):
    worker, config = example_worker
    worker.update_config({"params.message": "更新后", "params.log_every_n_calls": 2}, 2)
    worker.submit_command("hot", "example", [])
    reply = command_result(worker)
    assert reply["config_version"] == 2
    assert reply["result"]["message"] == "更新后" and reply["result"]["log_every_n_calls"] == 2
    worker.submit({**frame_request, "config_version": 2})
    assert wait_until(worker.poll, 5)["config_version"] == 2
    worker.submit_monitor(1)
    assert wait_until(worker.poll_monitor, 5)["result"]["config_version"] == 2
    before = copy.deepcopy(config)
    # Existing SDK closes a failed update session rather than guessing its ACK state.
    with pytest.raises(RuntimeError, match="log_every_n_calls"):
        worker.update_config({"params.message": "不可部分应用", "params.log_every_n_calls": 0}, 3)
    assert config == before and worker.config_version == 2
    assert worker.cleanup_confirmed and not worker.alive


def test_native_hold_example_chain_preserves_upstream_actions(tmp_path, frame_request, image_readers):
    root = tmp_path / "policy"
    for identity in ("hold", "example"):
        copy_templates(ROOT / "policy" / identity, root / identity)
    chain = root / "demo"
    chain.mkdir()
    (chain / "policy.yaml").write_text(json.dumps({
        "schema_version": 3, "kind": "chain", "policies": ["hold", "example", "example"],
    }))
    record = PolicyRegistry(root).get("demo")
    worker = ChainProcess(record, process_factory=worker_factory,
                          init_timeout_s=10, inference_timeout_s=5, release_timeout_s=2)
    image_readers.append(worker)
    try:
        worker.start()
        worker.submit(frame_request)
        result = wait_until(worker.poll, 6)
        assert result["type"] == "result", result
        assert result["images_released"] is True
        assert result["actions"] == [[0.25] * 14] * 50
        assert not result.get("control_requests")
        assert len(worker.monitor_members()) == 2
        worker.submit_command("count", "example-example", [])
        assert command_result(worker)["result"]["calls"] == 2
    finally:
        assert not worker.close()
