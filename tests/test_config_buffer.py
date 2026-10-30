from config_helpers import read_config
import copy
import json
from pathlib import Path

import numpy as np
import pytest

from robot_control.config import PolicyRegistry, validate_policy, changed_config
from robot_control.buffer import ActionBuffer
from robot_control.safety import SlewLimiter


def manifest():
    return {"schema_version": 3, "id": "hold",  "execution": {"mode": "async", "call_every_actions": 2, "control_mode": "qpos"}, "params": {"nested": {"value": 1}}, "hot_reloadable": ["params.nested.value"]}


def test_nested_hot_exact_and_original_not_mutated():
    original = manifest()
    changed, hot = changed_config(original, "params.nested.value", 2)
    assert hot and changed["params"]["nested"]["value"] == 2
    assert original["params"]["nested"]["value"] == 1
    assert not changed_config(original, "execution.mode", "sync")[1]
    with pytest.raises(ValueError):
        changed_config(original, "id", "other")
    with pytest.raises(ValueError):
        changed_config(original, "params.nested.typo", 2)


@pytest.mark.parametrize("value", [0, 1.5])
def test_invalid_interval_rejected(value):
    conf = manifest()
    conf["execution"]["call_every_actions"] = value
    with pytest.raises(ValueError):
        validate_policy(conf)


def test_process_parameters_cannot_be_declared_hot():
    conf = manifest()
    conf["hot_reloadable"] = ["policy_host"]
    with pytest.raises(ValueError):
        validate_policy(conf)


def test_registry_keeps_session_changes_and_atomic_save(tmp_path):
    folder = tmp_path / "hold"
    folder.mkdir()
    (folder / "main.py").write_text("\n".join(f"def {name}(*args): pass" for name in ["initialize", "policy", "reset_context", "release"]))
    (folder / "main.py").with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
    (folder / "policy.yaml").write_text(json.dumps(manifest()))
    registry = PolicyRegistry(tmp_path)
    record = registry.get("hold")
    updated, _ = changed_config(record.config, "params.nested.value", 9)
    registry.commit("hold", updated)
    assert registry.get("hold").config["params"]["nested"]["value"] == 9
    registry.save("hold")
    assert read_config(folder / "policy.yaml")["params"]["nested"]["value"] == 9


def test_invalid_manifest_and_entry_do_not_break_policy_discovery(tmp_path):
    valid = tmp_path / "valid"
    valid.mkdir()
    source = "\n".join(f"def {name}(*args): pass" for name in ["initialize", "policy", "reset_context", "release"])
    (valid / "main.py").write_text(source + "\nraise RuntimeError('static check must not import')\n")
    (valid / "main.py").with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
    (valid / "policy.yaml").write_text(json.dumps({**manifest(), "id": "valid"}))
    for name, payload in [("malformed", {**manifest(), "execution": []}), ("missing_entry", {**manifest(), "id": "missing_entry"})]:
        folder = tmp_path / name
        folder.mkdir()
        (folder / "policy.yaml").write_text(json.dumps(payload))
        if name != "missing_entry":
            (folder / "main.py").write_text("def policy(*args): pass\n")
            (folder / "main.py").with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
    registry = PolicyRegistry(tmp_path)
    assert sum(item["valid"] for item in registry.list()) == 1
    assert registry.get("valid").directory == valid


def test_consumed_counter_trims_exact_request_prefix():
    buffer = ActionBuffer()
    buffer.install(np.arange(84).reshape(6, 14))
    request = buffer.begin_request()
    buffer.consume()
    buffer.consume()
    replacement = np.arange(140).reshape(10, 14) + 1000
    assert buffer.accept(request, replacement)
    np.testing.assert_array_equal(buffer.remaining(), replacement[2:])
    assert buffer.consumed_since_request == 2


def test_stale_generation_and_complete_trim_do_not_install():
    buffer = ActionBuffer()
    buffer.install(np.zeros((3, 14)))
    request = buffer.begin_request()
    for _ in range(3):
        buffer.consume()
    assert not buffer.accept(request, np.zeros((3, 14)))
    buffer.invalidate()
    assert not buffer.accept(request, np.ones((6, 14)))
    assert len(buffer.remaining()) == 0


def test_slew_wall_preserves_direction_and_consumes_only_at_target():
    limiter = SlewLimiter({"enabled": True, "reference_frequency_hz": 50, "max_delta_per_step": [0.1] * 14}, 25)
    limiter.reset(np.zeros(14))
    action, consumed = limiter.apply(np.ones(14))
    np.testing.assert_allclose(action, 0.2)
    assert not consumed
    for _ in range(4):
        action, consumed = limiter.apply(np.ones(14))
    assert consumed
    np.testing.assert_allclose(action, 1)
