"""Behavior of the shipped, runnable example; no developer-private config."""
from config_helpers import read_config
import copy
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from rc_policy_sdk.configuration import apply_patch
from rc_policy_sdk.images import ImageBatch, ImagePool, map_images
from rc_policy_sdk.monitor import normalize_snapshot
from rc_policy_sdk.runtime import PolicyRuntime

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "policy" / "example"


@pytest.fixture
def example():
    path = EXAMPLE / "main.py"
    assert path.is_file(), "the runnable example policy must exist"
    spec = importlib.util.spec_from_file_location("example_policy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def config():
    return read_config(EXAMPLE / "policy.yaml.example")


@pytest.fixture
def images():
    batch = ImageBatch({"head": {"timestamp": 12.5}, "left": {"timestamp": 13.0}})
    for name, value in (("head", 7), ("left", 19)):
        batch[name] = np.full((2, 3, 3), value, dtype=np.uint8)
        batch[name].flags.writeable = False
    return batch


@pytest.fixture
def runtime():
    return PolicyRuntime(SimpleNamespace(runtime_tools=frozenset({"pause"})), {
        "config_version": 3,
        "runtime": {"observation_id": "sample-42", "consumed_actions": 5, "control_epoch": 9},
    })


@pytest.mark.parametrize("as_array", [False, True])
def test_nonempty_actions_return_by_identity_without_changes(example, config, images, runtime, as_array):
    actions = np.arange(42, dtype=np.float64).reshape(3, 14) / 7
    if not as_array:
        actions = actions.tolist()
    before = copy.deepcopy(actions)
    context = example.initialize(config)
    with runtime._inference_scope():
        output = example.policy(context, {}, actions, images, config, runtime=runtime)
    assert output is actions
    np.testing.assert_array_equal(actions, before)
    info = example.monitor(context)
    assert info["calls"] == 1
    assert info["observation_id"] == "sample-42"
    assert (info["consumed_actions"], info["control_epoch"], info["config_version"]) == (5, 9, 3)


@pytest.mark.parametrize("empty", [[], np.empty((0, 14))])
def test_empty_input_holds_one_current_pose(example, config, images, runtime, empty):
    state = {"qpos": np.linspace(-0.3, 0.3, 14).tolist()}
    before = copy.deepcopy(state)
    context = example.initialize(config)
    output = example.policy(context, state, empty, images, config, runtime=runtime)
    np.testing.assert_array_equal(output, [state["qpos"]])
    assert state == before and len(empty) == 0


def test_observe_command_and_monitor_are_detached_scalar_snapshots(example, config, images, runtime):
    context = example.initialize(config)
    assert example.wants_observations(context) is True
    assert example.observe(context, {}, images, config, runtime=runtime) is None
    snapshot = normalize_snapshot(example.monitor(context))
    assert snapshot["calls"] == 0 and snapshot["observations"] == 1
    assert snapshot["last_callback"] == "observe" and snapshot["image_count"] == 2
    metadata = json.loads(snapshot["images"])
    assert metadata["head"] == {"shape": [2, 3, 3], "dtype": "uint8", "timestamp": 12.5,
                                "readonly": True, "sample": "7"}
    assert metadata["left"]["sample"] == "19"
    assert example.example(context) == snapshot
    snapshot["observations"] = -1
    assert example.monitor(context)["observations"] == 1


def test_image_mappings_close_while_context_remains_alive(example, config, runtime, tmp_path):
    context = example.initialize(config)
    pool = ImagePool(tmp_path / "frames", cameras=("head",), slots=2)
    try:
        for pixel in (7, 8, 9, 10):
            assert pool.publish("head", np.full((2, 3, 3), pixel, np.uint8), float(pixel))
            lease = pool.acquire()
            try:
                with map_images(lease.descriptors) as mapped:
                    example.policy(context, {"qpos": [0] * 14}, [], mapped, config, runtime=runtime)
                    example.observe(context, {}, mapped, config, runtime=runtime)
                assert json.loads(example.monitor(context)["images"])["head"]["sample"] == str(pixel)
            finally:
                lease.release()
    finally:
        pool.close()


def test_hot_config_reference_and_log_interval(example, config, images, runtime, capsys):
    context = example.initialize(config)
    params = config["params"]
    example.validate_config(config)
    apply_patch(config, {"params.message": "热更新", "params.log_every_n_calls": 2})
    assert context["config"] is config and config["params"] is params
    capsys.readouterr()
    actions = [[0] * 14]
    example.policy(context, {}, actions, images, config, runtime=runtime)
    assert capsys.readouterr().out == ""
    example.policy(context, {}, actions, images, config, runtime=runtime)
    assert "热更新" in capsys.readouterr().out
    assert example.example(context)["message"] == "热更新"


@pytest.mark.parametrize("key,value", [("message", 1), ("message", None),
    ("log_every_n_calls", True), ("log_every_n_calls", 0), ("log_every_n_calls", -1),
    ("log_every_n_calls", 1.5), ("log_every_n_calls", "2")])
def test_invalid_business_config_is_rejected(example, config, key, value):
    config["params"][key] = value
    before = copy.deepcopy(config)
    with pytest.raises(ValueError, match=key):
        example.validate_config(config)
    with pytest.raises(ValueError, match=key):
        example.initialize(config)
    assert config == before


@pytest.mark.parametrize("mode", ["epos", "mixed"])
def test_qpos_fallback_cannot_be_mislabeled_as_another_mode(example, config, mode):
    config["execution"]["control_mode"] = mode
    with pytest.raises(ValueError, match="qpos"):
        example.initialize(config)


def test_reset_and_idempotent_release_keep_config_reference(example, config, images, runtime, capsys):
    context = example.initialize(config)
    example.policy(context, {"qpos": [0] * 14}, [], images, config, runtime=runtime)
    example.observe(context, {}, images, config, runtime=runtime)
    example.reset_context(context)
    assert context["config"] is config
    info = example.monitor(context)
    assert info["calls"] == info["observations"] == info["image_count"] == 0
    assert info["images"] == "{}" and info["observation_id"] == ""
    capsys.readouterr()
    example.release(context)
    assert capsys.readouterr().out
    example.release(context)
    example.release(None)
    assert capsys.readouterr().out == ""
    assert context["config"] is config


def test_parallel_observation_inference_and_reads_are_consistent(example, config, images, runtime):
    context = example.initialize(config)
    actions = [[0] * 14]

    def infer():
        for _ in range(30):
            assert example.policy(context, {}, actions, images, config, runtime=runtime) is actions

    def observe():
        for _ in range(30):
            example.observe(context, {}, images, config, runtime=runtime)

    def read():
        for _ in range(30):
            for snapshot in (example.monitor(context), example.example(context)):
                normalize_snapshot(snapshot)
                if snapshot["image_count"]:
                    assert len(json.loads(snapshot["images"])) == snapshot["image_count"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(function) for function in (infer, observe, read, read)]
        for future in futures:
            future.result(timeout=5)
    assert example.monitor(context)["calls"] == example.monitor(context)["observations"] == 30


def test_template_setup_and_catalog_discovery(tmp_path):
    import shutil
    import sys
    sys.path.insert(0, str(ROOT / "scripts"))
    from config_files import configuration_updates, write_updates
    from config_validation import validate_documents
    from setup_project import select_policies
    from robot_control.policy_catalog import discover
    from rc_policy_sdk.commands import command_specs

    assert EXAMPLE.is_dir(), "example must be a self-contained policy directory"
    shutil.copytree(EXAMPLE, tmp_path / "policy" / "example",
                    ignore=shutil.ignore_patterns("policy.yaml", "__pycache__"))
    assert discover(tmp_path / "policy")[0]["state"] == "needs_config"
    changes, documents = configuration_updates(tmp_path)
    validate_documents(documents)
    write_updates(tmp_path, changes)
    assert select_policies(tmp_path, ["example"]) == ["example"]
    row = discover(tmp_path / "policy")[0]
    assert row["id"] == "example" and row["valid"]
    config = documents["policy/example/policy.yaml"]
    assert command_specs(config)["example"]["args"] == []
    assert config["execution"] == {"mode": "sync", "call_every_actions": 1, "control_mode": "qpos"}
