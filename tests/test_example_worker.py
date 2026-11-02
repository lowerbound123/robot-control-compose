"""Shipped example through actual SDK workers, shared images and native chains."""
from config_helpers import read_config
import copy
import json
from pathlib import Path
import sys
import time

import numpy as np
import pytest

from rc_policy_sdk.images import ImagePool
from robot_control.chain_process import ChainProcess
from robot_control.config import PolicyRegistry
from robot_control.global_state import GlobalStateService
from host_helpers import LocalPolicyProcess
from project_templates import copy_templates
from test_policy_monitor_worker import wait_until

ROOT = Path(__file__).resolve().parents[1]


def worker_factory(*args, **kwargs):
    return LocalPolicyProcess(*args, python_command=[sys.executable], **kwargs)


def command_result(worker):
    result = command_reply(worker)
    assert result["type"] == "command_result", result
    return result


def command_reply(worker):
    def completed():
        message = worker.poll_command()
        return message if message and message["type"] != "command_started" else None
    return wait_until(completed, 5)


@pytest.fixture
def image_readers():
    return []


@pytest.fixture
def globals_service(tmp_path):
    service = GlobalStateService(tmp_path / "global.env", tmp_path / "ipc", run_id="example")
    service.start()
    try:
        yield service
    finally:
        service.close(time.monotonic() + 3)


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


def test_repeated_example_chain_preserves_actions_and_shares_only_explicit_state(
        tmp_path, frame_request, image_readers, globals_service):
    root = tmp_path / "policy"
    copy_templates(ROOT / "policy/example", root / "example")
    copy_templates(ROOT / "policy/example_chain", root / "example_chain")
    record = PolicyRegistry(root).get("example_chain")
    worker = ChainProcess(record, process_factory=worker_factory,
                          init_timeout_s=10, inference_timeout_s=5, release_timeout_s=2,
                          global_state=globals_service)
    image_readers.append(worker)
    try:
        worker.start()
        actions = [[.125] * 14, [-.375] * 14]
        worker.submit({**frame_request, "action_chunk": actions})
        result = wait_until(worker.poll, 6)
        assert result["type"] == "result", result
        assert result["images_released"] is True
        assert result["actions"] == actions
        assert not result.get("control_requests")
        assert set(worker.monitor_members()) == {"example-0", "example-1"}
        for instance in ("example-0", "example-1"):
            worker.submit_command(instance, instance + "-example", [])
            snapshot = command_result(worker)["result"]
            assert snapshot["calls"] == 1 and snapshot["instance"] == instance
        worker.submit_command("read", "example-0-shared-read", [])
        version = command_result(worker)["result"]["version"]
        worker.submit_command("write", "example-0-shared-write", ["来自首个实例", version])
        assert command_result(worker)["result"] == version + 1
        worker.submit_command("read2", "example-1-shared-read", [])
        assert command_result(worker)["result"] == {"value": "来自首个实例", "version": version + 1}
        worker.submit_command("stale", "example-1-shared-write", ["过期写入", version])
        reply = command_reply(worker)
        assert reply["type"] == "command_error" and "GlobalStateConflict" in reply["error"]
        assert globals_service.proxy["example_note"] == "来自首个实例"

        for instance, outcome in (("example-0", "completed"), ("example-1", "pause")):
            worker.submit_command("arm-" + instance, instance + "-next-result", [outcome, instance])
            assert command_result(worker)["result"] == {"queued": outcome}
        worker.submit({**frame_request, "request_id": 2})
        reply = wait_until(worker.poll, 6)
        assert reply["images_released"] and reply["control_requests"] == [{
            "type": "pause", "reason": "example-1", "source": {"policy_id": "example-1", "cell": 2}}]
    finally:
        assert not worker.close()
