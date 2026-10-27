import sys
import threading
import time
from pathlib import Path

import pytest
import numpy as np
from rc_policy_sdk.images import ImagePool

from host_helpers import LocalPolicyProcess as PolicyProcess

SOURCE = '''
import time
from pathlib import Path
def initialize(config): return {"root":Path(config["params"]["root"]), "calls":0, "config":config}
def policy(context, state, action_chunk, images, config):
    (context["root"] / "infer").touch()
    time.sleep(.4)
    return [[0] * 14]
def monitor(context):
    root = context["root"]
    context["calls"] += 1
    (root / "reading").touch()
    while (root / "hang").exists(): time.sleep(.01)
    (root / "reading").unlink()
    if (root / "invalid").exists(): return {"bad": True}
    return {"count":context["calls"], "text":"中文", "version":context["config"]["params"].get("value",0)}
def reset_context(context):
    assert not (context["root"] / "reading").exists()
    context["calls"] = 0
def release(context):
    assert not (context["root"] / "reading").exists()
    (context["root"] / "released").touch()
'''


def wait_until(callback, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = callback()
        if result: return result
        time.sleep(.005)
    raise AssertionError("condition did not complete")


@pytest.fixture
def worker(tmp_path):
    (tmp_path / "main.py").write_text(SOURCE)
    (tmp_path / "main.py").with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
    config = {"id":"monitored",  "params":{"root":str(tmp_path),"value":0}, "hot_reloadable":["params.value"], "execution":{"control_mode":"qpos"}}
    process = PolicyProcess(tmp_path, config, python_command=[sys.executable], inference_timeout_s=3, release_timeout_s=.25)
    process.start()
    try: yield process, tmp_path
    finally:
        (tmp_path / "hang").unlink(missing_ok=True)
        process.close()


def test_monitor_capability_and_concurrent_inference(worker):
    process, root = worker
    assert process.supports_monitor
    pool = ImagePool(root / "frames", cameras=("head",))
    pool.publish("head", np.zeros((2, 2, 3), np.uint8), 1.)
    lease = pool.acquire()
    process.submit(dict(request_id=1, generation=0, config_version=1, state={}, action_chunk=[], images=lease.descriptors))
    wait_until(lambda: (root / "infer").exists())
    assert process.submit_monitor(0)
    result = wait_until(process.poll_monitor)
    assert result["type"] == "monitor_result" and result["result"]["count"] == 1
    assert process.poll() is None  # inference is still sleeping
    assert "images_released" not in result
    assert wait_until(process.poll)["type"] == "result"
    lease.release()
    pool.close()


def test_monitor_error_is_not_policy_failure_and_reset_waits(worker):
    process, root = worker
    (root / "invalid").touch()
    process.submit_monitor(1)
    assert wait_until(process.poll_monitor)["type"] == "monitor_error"
    (root / "invalid").unlink()
    (root / "hang").touch()
    process.submit_monitor(1)
    wait_until(lambda: (root / "reading").exists())
    assert not process.submit_monitor(2)
    reset = threading.Thread(target=process.reset_context)
    reset.start()
    time.sleep(.05)
    assert reset.is_alive()
    (root / "hang").unlink()
    reset.join(2)
    assert not reset.is_alive()
    wait_until(process.poll_monitor)
    assert process.submit_monitor(2)
    assert wait_until(process.poll_monitor)["result"]["count"] == 1
    assert process.alive


def test_hot_config_waits_for_reader_and_next_snapshot_uses_new_version(worker):
    process, root = worker
    (root / "hang").touch()
    process.submit_monitor(1)
    wait_until(lambda: (root / "reading").exists())
    errors = []
    def update():
        try: process.update_config({"params.value":7}, 2)
        except Exception as exc: errors.append(exc)
    updater = threading.Thread(target=update)
    updater.start()
    time.sleep(.05)
    assert updater.is_alive()
    (root / "hang").unlink()
    updater.join(2)
    assert not updater.is_alive() and not errors
    assert wait_until(process.poll_monitor)["config_version"] == 1
    assert process.submit_monitor(2)
    reply = wait_until(process.poll_monitor)
    assert reply["config_version"] == 2 and reply["result"]["version"] == 7


def test_hanging_monitor_does_not_fault_until_bounded_release(worker):
    process, root = worker
    (root / "hang").touch()
    process.submit_monitor(1)
    wait_until(lambda: (root / "reading").exists())
    time.sleep(1.05)
    assert process.alive
    assert process.poll() is None
    start = time.monotonic()
    assert process.close()  # forced cleanup, never release context under the reader
    assert time.monotonic() - start < 7
    assert not process.alive
    assert not (root / "released").exists()


def test_missing_callback_is_backward_compatible(tmp_path):
    (tmp_path / "main.py").write_text(SOURCE.replace("def monitor(context):", "def unused(context):"))
    (tmp_path / "main.py").with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
    cfg = {"id":"legacy",  "params":{"root":str(tmp_path)}}
    process = PolicyProcess(tmp_path, cfg, python_command=[sys.executable], release_timeout_s=.25)
    try:
        process.start()
        assert not process.supports_monitor
        assert not process.submit_monitor(0)
        process.reset_context()
    finally: process.close()


def test_ready_contains_all_callback_capabilities_before_followup_messages(tmp_path):
    source = SOURCE + '''
def initialize(config, *, runtime):
    send = runtime.channel.send
    def delay_capabilities(message):
        if message.get("type") != "capabilities": send(message)
    runtime.channel.send = delay_capabilities
    return {"root":Path(config["params"]["root"]), "calls":0, "config":config}
def observe(context, state, images, config): pass
'''
    (tmp_path / "main.py").write_text(source)
    (tmp_path / "main.py").with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
    cfg = {"id":"ready-caps",  "params":{"root":str(tmp_path)}}
    process = PolicyProcess(tmp_path, cfg, python_command=[sys.executable], release_timeout_s=.25)
    try:
        process.start()
        assert process.supports_monitor
        assert process.supports_observe, "start must publish the complete capabilities atomically"
    finally: process.close()
