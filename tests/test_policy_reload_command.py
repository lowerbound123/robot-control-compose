"""Reload effects exercise real worker exit/initialization and controller receipts."""
import json
import sys

import pytest

from rc_policy_sdk.commands import command_specs
from robot_control.controller import RobotController
from host_helpers import LocalPolicyProcess as PolicyProcess
from robot_control.robots import MockRobot
from test_controller import until
from test_command_controller import completed

SOURCE = '''
import json, os, time
from pathlib import Path

def initialize(config):
    root = Path(config["params"]["root"])
    model = json.loads(Path(config["params"]["model_config_path"]).read_text())
    with (root / "events").open("a") as f: f.write("load:" + model["name"] + "\\n")
    time.sleep(model.get("load_delay", 0))
    if model.get("fail"): raise ValueError("new model initialization failed")
    return {"root": root, "name": model["name"], "pid": os.getpid()}
def policy(context, state, action_chunk, images, config): return [[.01] * 14]
def reset_context(context): (context["root"] / "reset").touch()
def release(context):
    with (context["root"] / "events").open("a") as f: f.write("release:" + context["name"] + "\\n")
def current(context): return {"name": context["name"], "pid": context["pid"]}
def switch_model(context, value):
    path = Path(value).resolve()
    model = json.loads(path.read_text())
    if model.get("invalid"): raise ValueError("invalid model configuration")
    (context["root"] / "validating").touch()
    time.sleep(model.get("validation_delay", 0))
    return {"params.model_config_path": str(path)}
def patch(context, value): return json.loads(value)
def wait_command(context): time.sleep(.18)
'''


@pytest.fixture
def reload_controller(tmp_path, request):
    root = tmp_path / "policy" / "first"
    root.mkdir(parents=True)
    model = root / "model.json"
    model.write_text(json.dumps({"name": "old"}))
    (root / "main.py").write_text(SOURCE)
    (root / "main.py").with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
    config = {"schema_version": 3, "id": "first",
              "execution": {"mode": "sync", "call_every_actions": 1, "control_mode": "qpos"},
              "params": {"root": str(root), "model_config_path": str(model), "hot": 1, "nested": {"leaf": 1}},
              "hot_reloadable": ["params.hot"], "tui_commands": {
                  "switch-model": {"handler": "switch_model", "args": ["str"], "timeout_s": .2, "effect": "reload_policy"},
                  "patch": {"handler": "patch", "args": ["str"], "effect": "reload_policy"},
                  "current": {"handler": "current", "args": []},
                  "wait-command": {"handler": "wait_command", "args": []}}}
    (root / "policy.yaml").write_text(json.dumps(config))
    rc = {"schema_version": 3, "frequency_hz": 100, "policy_directory": "policy", "startup_policy": "first",
          "robot": {"backend": "mock"}, "safety": {"enabled": False, "reference_frequency_hz": 50, "max_delta_per_step": [.1]*14},
          "reset_qpos": [0]*14, "outputs_directory": "outputs",
          "timeouts": {"initialize_s": getattr(request, "param", 3), "inference_s": .1, "release_s": .3}}
    path = tmp_path / "rc.yaml"
    path.write_text(json.dumps(rc))
    def factory(*args, **kwargs): return PolicyProcess(*args, python_command=[sys.executable], **kwargs)
    controller = RobotController(path, robot=MockRobot(), process_factory=factory)
    controller.start()
    try:
        try:
            until(lambda: controller.status()["state"] == "paused" and controller._worker is not None, 5)
        except AssertionError:
            pytest.fail(str(controller.read_events()))
        yield controller, root, model
    finally:
        controller.submit("exit")
        assert controller.done.wait(10)


def test_reload_effect_manifest_is_preserved_and_unknown_effect_rejected():
    spec = {"handler": "change", "args": [], "effect": "reload_policy"}
    assert command_specs({"tui_commands": {"change": spec}})["change"]["effect"] == "reload_policy"
    with pytest.raises(ValueError):
        command_specs({"tui_commands": {"change": {**spec, "effect": "restart_robot"}}})


def test_same_path_forces_new_model_exit_then_initialize_and_paused_receipt(reload_controller):
    controller, root, model = reload_controller
    old_worker = controller._worker
    old_pid = completed(controller, controller.submit("current"))["result"]["pid"]
    completed(controller, controller.submit("resume"))
    until(lambda: controller.status()["consumed"] > 1)
    (root / "reset").unlink()
    model.write_text(json.dumps({"name": "new", "load_delay": .4}))
    call = controller.submit(f"switch-model {model}")
    until(lambda: "load:new" in (root / "events").read_text())
    assert controller.get_operation(call)["status"] == "running"
    assert not old_worker.alive
    result = completed(controller, call)
    assert result["status"] == "succeeded"
    assert result["operation_id"] == call
    assert controller.status()["state"] == "paused"
    assert controller.registry.get("first").version == 2
    assert not (root / "reset").exists()
    current = completed(controller, controller.submit("current"))["result"]
    assert current["name"] == "new" and current["pid"] != old_pid
    assert (root / "events").read_text().splitlines() == ["load:old", "release:old", "load:new"]


@pytest.mark.parametrize("patch", [{}, [], {"params.missing": 1}, {"params.hot": 2}, {"id": "other"},
                                  {"execution.call_every_actions": 2}, {"params.nested": {}}, {"params.model_config_path": {}}])
def test_invalid_reload_patch_preserves_worker(reload_controller, patch):
    import shlex
    controller, root, _ = reload_controller
    worker = controller._worker
    result = completed(controller, controller.submit("patch " + shlex.quote(json.dumps(patch))))
    assert result["status"] == "failed"
    assert controller._worker is worker and worker.alive
    assert controller.registry.get("first").version == 1
    assert (root / "events").read_text().splitlines() == ["load:old"]


def test_handler_validation_failure_preserves_old_model(reload_controller):
    controller, root, model = reload_controller
    model.write_text(json.dumps({"invalid": True}))
    worker = controller._worker
    assert completed(controller, controller.submit(f"switch-model {model}"))["status"] == "failed"
    assert controller._worker is worker and worker.alive
    assert completed(controller, controller.submit("current"))["result"]["name"] == "old"


@pytest.mark.parametrize("interruption", ["pause", "take", "exit"])
def test_result_after_control_interruption_cannot_reload(reload_controller, interruption):
    controller, root, model = reload_controller
    worker = controller._worker
    model.write_text(json.dumps({"name": "new", "validation_delay": .15}))
    call = controller.submit(f"switch-model {model}")
    until(lambda: (root / "validating").exists())
    completed(controller, controller.submit(interruption))
    assert completed(controller, call)["status"] == "cancelled"
    assert "load:new" not in (root / "events").read_text()
    if interruption != "exit": assert controller._worker is worker and worker.alive


def test_reload_is_rejected_during_mcp_takeover_and_queued_before_take(reload_controller):
    controller, root, model = reload_controller
    with controller._lock:
        call = controller.submit(f"switch-model {model}")
        take = controller.submit("take")
    completed(controller, take)
    assert completed(controller, call)["status"] == "cancelled"
    assert completed(controller, controller.submit(f"switch-model {model}"))["status"] == "failed"
    assert not (root / "validating").exists()


@pytest.mark.parametrize("reload_controller, model_config", [(3, {"name": "bad", "fail": True}),
                                                              (.5, {"name": "slow", "load_delay": 2})], indirect=["reload_controller"])
def test_new_initialization_failure_or_timeout_faults_without_rollback(reload_controller, model_config):
    controller, root, model = reload_controller
    worker = controller._worker
    model.write_text(json.dumps(model_config))
    result = completed(controller, controller.submit(f"switch-model {model}"), 7)
    assert result["status"] == "failed"
    assert controller.status()["state"] == "fault"
    assert not worker.alive
    assert controller.registry.get("first").version == 1
    assert (root / "events").read_text().count("load:old") == 1


def test_new_worker_rejects_old_host_before_initializing_model(reload_controller):
    import socket, threading, uuid
    from rc_policy_sdk.worker import run_session
    from rc_policy_sdk.wire import send_message, receive_message
    controller, root, _ = reload_controller
    client, server = socket.socketpair()
    thread=threading.Thread(target=run_session,args=(server,root,'rc_test_'+uuid.uuid4().hex))
    thread.start()
    try:
        send_message(client, {'type':'initialize','config_version':1,'config':controller.registry.get('first').config})
        response=receive_message(client)
        assert response['type']=='error' and 'reload_policy' in response['error']
        assert (root/'events').read_text().splitlines()==['load:old']
    finally:
        client.close();thread.join(2)
    assert not thread.is_alive()


def test_reload_validation_timeout_kills_worker_and_never_loads_new_model(reload_controller):
    controller, root, model = reload_controller
    worker = controller._worker
    model.write_text(json.dumps({"name": "new", "validation_delay": 2}))
    result = completed(controller, controller.submit(f"switch-model {model}"))
    assert result["status"] == "failed" and "timeout" in result["error"]
    until(lambda: controller._worker is None, 8)
    assert not worker.alive
    assert "load:new" not in (root / "events").read_text()
    assert controller.status()["state"] == "paused"


def test_explicit_switch_during_validation_cannot_apply_stale_reload(reload_controller):
    controller, root, model = reload_controller
    model.write_text(json.dumps({"name": "new", "validation_delay": .15}))
    call = controller.submit(f"switch-model {model}")
    until(lambda: (root / "validating").exists())
    switched = controller.submit("switch first")
    assert completed(controller, switched)["status"] == "succeeded"
    assert completed(controller, call)["status"] == "cancelled"
    assert controller.registry.get("first").version == 2
    assert (root / "events").read_text().count("load:new") == 1


def test_multiple_reload_results_only_apply_one_lifecycle(reload_controller):
    controller, root, model = reload_controller
    model.write_text(json.dumps({"name": "new", "validation_delay": .12}))
    with controller._lock:
        calls = [controller.submit(f"switch-model {model}") for _ in range(2)]
    results = [completed(controller, call)["status"] for call in calls]
    assert sorted(results) == ["cancelled", "succeeded"]
    assert controller.registry.get("first").version == 2
    assert (root / "events").read_text().count("load:new") == 1


def test_hot_config_lifecycle_cancels_reload_result_from_previous_version(reload_controller):
    controller, root, model = reload_controller
    worker = controller._worker
    # The hot transaction must have enough time to drain the active validator.
    worker.inference_timeout_s = 1
    model.write_text(json.dumps({"name": "new", "validation_delay": .15}))
    call = controller.submit(f"switch-model {model}")
    until(lambda: (root / "validating").exists())
    update = controller.submit("set policy.params.hot 2")
    assert completed(controller, update)["status"] == "succeeded"
    assert completed(controller, call)["status"] == "cancelled"
    assert controller._worker is worker and worker.alive
    assert worker.config_version == controller.registry.get("first").version == 2
    assert "load:new" not in (root / "events").read_text()


def test_failed_reload_dispatch_releases_pending_snapshot(reload_controller):
    controller, _, model = reload_controller
    worker = controller._worker
    # Reproduce a worker gate rejection after UI acceptance, e.g. a lifecycle
    # gate sent by an earlier operation. Failed receipts must retain no request.
    with controller._lock:
        worker.stop_commands()
        call = controller.submit(f"switch-model {model}")
    assert completed(controller, call)["status"] == "failed"
    assert call not in controller._policy_reloads
    worker.resume_commands()
    assert completed(controller, controller.submit("current"))["result"]["name"] == "old"
