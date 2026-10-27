import sys
import time

import numpy as np
import pytest
from rc_policy_sdk.images import ImagePool

from host_helpers import LocalPolicyProcess as PolicyProcess

SOURCE = '''
import threading
import time
from pathlib import Path

def initialize(config):
    return {"barrier": threading.Barrier(3), "root": Path(config["params"]["root"])}
def policy(context, state, action_chunk, images, config):
    context["barrier"].wait(3)
    return [[0] * 14]
def meet(context, value):
    context["barrier"].wait(3)
    return value

def delay(context, label, seconds):
    (context["root"] / (label + ".start")).touch()
    time.sleep(seconds)
    (context["root"] / (label + ".end")).touch()
    return label

def ping(context): return "pong"
def duplicate(context): return {1: "integer", "1": "string"}
def huge_error(context): raise ValueError("x" * (4 * 1024 * 1024 + 100))
def surrogate_error(context): raise ValueError(chr(0xd800))
def broken_error(context):
    class Broken(Exception):
        def __str__(self): raise RuntimeError("broken __str__")
    raise Broken()
def die(context):
    import os
    os._exit(17)
def bad(context): raise ValueError("intentional failure")
def invalid(context): return float("nan")
def reset_context(context):
    assert not (context["root"] / "active.start").exists() or (context["root"] / "active.end").exists()
    (context["root"] / "reset").touch()
def release(context):
    (context["root"] / "release").touch()
'''


def worker_config(root, workers=4):
    return {"id": "commands",  "params": {"root": str(root)},
            "execution": {"control_mode": "qpos", "tui_command_workers": workers},
            "tui_commands": {
                "meet": {"handler": "meet", "args": ["str"]},
                "delay": {"handler": "delay", "args": ["str", "float"], "timeout_s": .15},
                "unlimited": {"handler": "delay", "args": ["str", "float"]},
                "bad": {"handler": "bad", "args": []},
                "die": {"handler": "die", "args": []},
                "direct": {"handler": "ping", "args": []},
                "duplicate": {"handler": "duplicate", "args": []},
                "huge_error": {"handler": "huge_error", "args": []},
                "surrogate_error": {"handler": "surrogate_error", "args": []},
                "broken_error": {"handler": "broken_error", "args": []},
                "invalid": {"handler": "invalid", "args": []}}}


@pytest.fixture
def make_worker(tmp_path):
    made = []
    def make(workers=4, source=SOURCE, config=None):
        directory = tmp_path / str(len(made))
        directory.mkdir()
        (directory / "main.py").write_text(source)
        (directory / "main.py").with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
        worker = PolicyProcess(directory, config or worker_config(directory, workers),
                               python_command=[sys.executable], release_timeout_s=.2, inference_timeout_s=3)
        made.append(worker)
        worker.start()
        return worker, directory
    yield make
    for worker in made:
        worker.close()


def wait_commands(worker, count, timeout=4):
    results = {}
    deadline = time.monotonic() + timeout
    while len(results) < count and time.monotonic() < deadline:
        message = worker.poll_command()
        if message and message["type"] != "command_started":
            results[message["command_id"]] = message
        time.sleep(.005)
    assert len(results) == count, results
    return results


def test_commands_overlap_inference_and_each_other(make_worker, tmp_path):
    worker, _ = make_worker()
    pool = ImagePool(tmp_path / "frames", cameras=("head",))
    pool.publish("head", np.zeros((2, 2, 3), np.uint8), 1.)
    lease = pool.acquire()
    worker.submit({"request_id": 1, "generation": 0, "config_version": 1,
                   "state": {}, "action_chunk": [], "images": lease.descriptors})
    worker.submit_command("one", "meet", ["first"])
    worker.submit_command("two", "meet", ["second"])
    results = wait_commands(worker, 2)
    assert results["one"]["result"] == "first"
    assert results["two"]["result"] == "second"
    deadline = time.monotonic() + 2
    prediction = None
    while prediction is None and time.monotonic() < deadline:
        prediction = worker.poll()
        time.sleep(.005)
    assert prediction["type"] == "result"
    lease.release()
    pool.close()


def test_errors_do_not_kill_worker_or_other_calls(make_worker):
    worker, _ = make_worker()
    for identity in ("bad", "invalid"):
        worker.submit_command(identity, identity, [])
    worker.submit_command("ok", "unlimited", ["ok", .02])
    results = wait_commands(worker, 3)
    assert results["bad"]["type"] == results["invalid"]["type"] == "command_error"
    assert results["ok"]["result"] == "ok"
    assert worker.alive
    worker.reset_context()


def test_queued_time_is_excluded_and_minus_one_is_unlimited(make_worker):
    worker, _ = make_worker(workers=1)
    worker.submit_command("long", "unlimited", ["long", .3])
    worker.submit_command("queued", "delay", ["short", .03])
    results = wait_commands(worker, 2)
    assert all(item["type"] == "command_result" for item in results.values())


def test_deadline_is_reported_even_if_result_arrived_before_poll(make_worker):
    worker, directory = make_worker()
    worker.submit_command("slow", "delay", ["slow", .2])
    deadline = time.monotonic() + 2
    while not (directory / "slow.end").exists() and time.monotonic() < deadline:
        time.sleep(.01)
    results = wait_commands(worker, 1)
    assert results["slow"]["type"] == "command_timeout"


def test_lifecycle_cancels_queued_and_waits_for_active(make_worker):
    worker, directory = make_worker(workers=1)
    worker.submit_command("active", "unlimited", ["active", .2])
    deadline = time.monotonic() + 2
    while not (directory / "active.start").exists() and time.monotonic() < deadline:
        time.sleep(.005)
    worker.submit_command("queued", "unlimited", ["queued", .01])
    worker.reset_context()
    results = wait_commands(worker, 2)
    assert results["active"]["type"] == "command_result"
    assert results["queued"]["type"] == "command_cancelled"
    assert not (directory / "queued.start").exists()
    assert (directory / "reset").exists()
    worker.submit_command("after", "unlimited", ["after", .01])
    assert wait_commands(worker, 1)["after"]["result"] == "after"


def test_invalid_handler_fails_start_without_initializing(tmp_path):
    directory = tmp_path / "bad"
    directory.mkdir()
    (directory / "main.py").write_text(SOURCE)
    (directory / "main.py").with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
    config = worker_config(directory)
    config["tui_commands"]["meet"]["handler"] = "missing"
    worker = PolicyProcess(directory, config, python_command=[sys.executable])
    try:
        with pytest.raises(RuntimeError, match="handler"):
            worker.start()
    finally:
        worker.close()


@pytest.mark.parametrize("with_commands", [False, True])
def test_legacy_sdk_capability_gate(tmp_path, with_commands):
    import socket, tempfile, threading
    from rc_policy_sdk.wire import receive_message, send_message
    from robot_control.host_client import HostClient
    with tempfile.TemporaryDirectory(prefix='oldrpc-', dir='/tmp') as directory:
        path = directory + '/s'
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(path); listener.listen(1)
            def legacy():
                connection, _ = listener.accept()
                with connection:
                    receive_message(connection)
                    message = ({'type':'rpc_result','rpc_id':0,'result':{'host_id':'old','protocol_version':0}}
                               if with_commands else {'type':'ready','config_version':1})
                    send_message(connection,message)
            thread=threading.Thread(target=legacy);thread.start()
            try:
                with pytest.raises(RuntimeError, match='protocol'):
                    HostClient.get(path,'old')
            finally: thread.join(2)


def test_hot_update_drains_active_commands_and_advances_command_version(make_worker, tmp_path):
    config = worker_config(tmp_path, workers=1)
    config["params"]["value"] = 1
    config["hot_reloadable"] = ["params.value"]
    worker, _ = make_worker(config=config)
    worker.submit_command("active", "unlimited", ["active", .2])
    deadline = time.monotonic() + 2
    while not (tmp_path / "active.start").exists() and time.monotonic() < deadline:
        time.sleep(.005)
    worker.submit_command("queued", "unlimited", ["queued", .01])
    worker.update_config({"params.value": 2}, 2)
    results = wait_commands(worker, 2)
    assert results["active"]["type"] == "command_result"
    assert results["active"]["config_version"] == 1
    assert results["queued"]["type"] == "command_cancelled"
    assert not (tmp_path / "queued.start").exists()
    worker.submit_command("after", "unlimited", ["after", .01])
    result = wait_commands(worker, 1)["after"]
    assert result["result"] == "after"
    assert result["config_version"] == 2
