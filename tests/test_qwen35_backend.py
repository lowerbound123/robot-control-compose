"""CPU checks exercise real tensor preprocessing; GPU/model weights are not loaded."""

import importlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages/qwen35-policy/src"
sys.path.insert(0, str(PACKAGE))


def backend_module():
    assert importlib.util.find_spec("rc_qwen35_policy.backend") is not None, (
        "Qwen35 backend missing"
    )
    return importlib.import_module("rc_qwen35_policy.backend")


def test_backend_import_does_not_import_model_stack():
    backend_module()
    code = "import sys; import rc_qwen35_policy.backend; assert 'torch' not in sys.modules; assert 'transformers' not in sys.modules; assert 'lerobot' not in sys.modules"
    subprocess.run(
        [sys.executable, "-c", code],
        env={**__import__("os").environ, "PYTHONPATH": str(PACKAGE)},
        check=True,
    )


def make_backend(workflow="single"):
    return backend_module().Qwen35Backend(
        {
            "model": {
                "checkpoint": "/tmp/checkpoint",
                "workflow": workflow,
                "dtype": "bfloat16",
                "action_mode": "delta",
                "state_offsets": {"0": 0.5},
                "subtask_refresh_chunks": 3,
                "subtask_max_new_tokens": 17,
            }
        }
    )


def test_stats_support_flat_and_split_layout_without_external_dataset_dependency(
    tmp_path,
):
    b = make_backend()
    b.checkpoint = tmp_path
    vals = {
        "min": list(range(14)),
        "max": list(range(14)),
        "mean": list(range(14)),
        "std": [2] * 14,
    }
    (tmp_path / "stats.json").write_text(
        json.dumps({"arx_acone": {"observation.state": vals, "action": vals}})
    )
    state, action = b.read_stats()
    np.testing.assert_equal(state["observation.state"]["mean"], np.arange(14))
    split = {}
    for prefix in ("states", "actions"):
        for name, sl in [
            ("left_joint", slice(0, 6)),
            ("left_gripper", slice(6, 7)),
            ("right_joint", slice(7, 13)),
            ("right_gripper", slice(13, 14)),
        ]:
            split[f"{prefix}.{name}.position"] = {k: v[sl] for k, v in vals.items()}
    (tmp_path / "stats.json").write_text(json.dumps({"arx_acone": split}))
    state2, action2 = b.read_stats()
    np.testing.assert_equal(state2, state)
    np.testing.assert_equal(action2, action)


@pytest.fixture
def cpu_torch(monkeypatch):
    torch = pytest.importorskip("torch")
    monkeypatch.setattr(torch.Tensor, "cuda", lambda self, *a, **k: self)
    return torch


def test_finalize_mapping_unnormalize_delta_grippers_and_no_mutation(cpu_torch):
    torch = cpu_torch
    b = make_backend()
    b.unnormalize = lambda data: {"action": data["action"] * 2 + 0.5}
    qpos = np.arange(14, dtype=np.float32)
    original = qpos.copy()
    prediction = torch.arange(16).repeat(1, 50, 1).float()
    actual = b.finalize_actions(prediction, qpos)
    expected = (
        np.array([0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13, 15], dtype=np.float32) * 2
        + 0.5
    )
    anchor = original.copy()
    anchor[[6, 13]] = 0
    np.testing.assert_equal(actual, np.tile(expected + anchor, (50, 1)))
    np.testing.assert_equal(qpos, original)
    assert actual.dtype == np.float32
    with pytest.raises(ValueError, match="finite"):
        b.finalize_actions(prediction * float("nan"), qpos)
    with pytest.raises(ValueError, match="shape"):
        b.finalize_actions(prediction[:, :2], qpos)


def test_prepare_rgb_channel_order_offsets_and_batch_dtypes(cpu_torch):
    torch = cpu_torch
    b = make_backend()
    b.input_transforms = lambda sample: {
        **sample,
        "integer": torch.tensor([3]),
        "mask": torch.tensor([True]),
    }
    image = np.zeros((2, 3, 3), dtype=np.uint8)
    image[..., 0] = 255
    qpos = np.arange(14, dtype=np.float32)
    actual = b._prepare_inputs(
        qpos, {k: image for k in ("head", "left_wrist", "right_wrist")}, "pick"
    )
    rgb = actual["images.rgb.head"]
    assert tuple(rgb.shape) == (1, 3, 2, 3)
    assert torch.all(rgb[:, 0] == 1) and torch.all(rgb[:, 1:] == 0)
    assert actual["observation.state"][0, 0].item() == 0.5
    assert qpos[0] == 0 and image[0, 0, 0] == 255
    assert actual["integer"].dtype == torch.int64 and actual["mask"].dtype == torch.bool
    assert actual["task"] == ["pick"]


def test_two_stage_generates_every_three_predictions_and_reset(cpu_torch, monkeypatch):
    torch = cpu_torch
    b = make_backend("two_stage")
    # Only replace Transformers' stopping criteria boundary, not generation/cache logic.
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(StoppingCriteria=object, StoppingCriteriaList=list),
    )
    count = []

    def generate(**kw):
        assert not torch.is_grad_enabled()
        assert kw["max_new_tokens"] == 17
        assert tuple(kw["pixel_values"].shape) == (6, 4)
        assert tuple(kw["image_grid_thw"].shape) == (3, 3)
        count.append(kw)
        return torch.cat([kw["input_ids"], torch.tensor([[12, 13, 100, 88]])], 1)

    seen = []

    def predict(inputs):
        assert not torch.is_grad_enabled()
        seen.append(inputs["observation.input_ids"].tolist())
        assert not inputs["observation.fast_token_mask"].any()
        return torch.zeros(1, 50, 16)

    b.policy = SimpleNamespace(
        model=SimpleNamespace(
            qwen3_5_with_expert=SimpleNamespace(
                qwen3_5=SimpleNamespace(generate=generate)
            )
        ),
        predict_action_chunk=predict,
    )
    b.policy_config = SimpleNamespace(action_token_min=100, action_token_max=110)
    b.debug_tokenizer = SimpleNamespace(decode=lambda ids, **kw: "subtask")
    b.unnormalize = lambda data: data

    def inputs(*args):
        return {
            "observation.input_ids": torch.tensor([[1, 2]]),
            "observation.attention_mask": torch.ones(1, 2, dtype=torch.long),
            "observation.pixel_values": torch.ones(1, 6, 4),
            "observation.image_grid_thw": torch.ones(1, 3, 3, dtype=torch.long),
        }

    monkeypatch.setattr(b, "_prepare_inputs", inputs)
    for _ in range(4):
        b.predict(np.zeros(14), {}, "task")
    assert len(count) == 2 and seen == [[[1, 2, 12, 13]]] * 4
    b.reset_context()
    b.predict(np.zeros(14), {}, "task")
    assert len(count) == 3
    b.close()
    b.close()
    assert b.policy is None and b._subtask_tokens is None


def test_vendor_is_self_contained_and_patch_is_pinned():
    vendor = ROOT / "vendor/qwen35"
    assert (vendor / "pyproject.toml").is_file(), "installable vendor runtime missing"
    assert (vendor / "SOURCE_MANIFEST.json").is_file()
    assert (ROOT / "scripts/install_qwen35_patches.py").is_file()


def test_patch_validation_is_read_only_and_requires_current_source(tmp_path, monkeypatch):
    from rc_qwen35_policy import patches as patch
    package = tmp_path / "transformers"
    target = package / "models/qwen3_5/modeling_qwen3_5.py"
    target.parent.mkdir(parents=True)
    target.write_text("original")
    monkeypatch.setattr(patch.metadata, "version", lambda name: "5.3.0")
    with pytest.raises(RuntimeError, match="5.2.0"):
        patch.check()
    assert target.read_text() == "original"
    monkeypatch.setattr(patch.metadata, "version", lambda name: "5.2.0")
    monkeypatch.setattr(patch.util, "find_spec", lambda name: SimpleNamespace(origin=str(package / "__init__.py")))
    with pytest.raises(RuntimeError, match="missing or differs"):
        patch.check()
    assert target.read_text() == "original"
    # Simulate a direct file mount without asking the validator to copy anything.
    target.write_bytes(patch.SOURCE.read_bytes())
    assert patch.check() == target
    assert not target.with_suffix(".py.pre-rc-qwen35").exists()
    source = tmp_path / "new-source.py"
    source.write_text("updated patch")
    monkeypatch.setattr(patch, 'SOURCE', source)
    with pytest.raises(RuntimeError, match="missing or differs"):
        patch.check()
    target.unlink()
    with pytest.raises(FileNotFoundError):
        patch.check()


def test_empty_generated_subtask_and_fast_prompt_stripping(cpu_torch, monkeypatch):
    torch = cpu_torch
    b = make_backend("two_stage")
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        SimpleNamespace(StoppingCriteria=object, StoppingCriteriaList=list),
    )
    inputs = {
        "observation.input_ids": torch.tensor([[1, 101, 2]]),
        "observation.attention_mask": torch.ones(1, 3, dtype=torch.long),
        "observation.pixel_values": torch.ones(3, 4),
        "observation.image_grid_thw": torch.ones(3, 3, dtype=torch.long),
    }
    seen = {}

    def predict_action_chunk(value):
        seen["input_ids"] = value["observation.input_ids"].tolist()
        seen["attention_mask"] = value["observation.attention_mask"].tolist()
        seen["fast_token_mask"] = value["observation.fast_token_mask"]
        return torch.zeros(1, 50, 16)

    b.policy = SimpleNamespace(
        model=SimpleNamespace(
            qwen3_5_with_expert=SimpleNamespace(
                qwen3_5=SimpleNamespace(generate=lambda **kw: kw["input_ids"])
            )
        ),
        predict_action_chunk=predict_action_chunk,
    )
    b.policy_config = SimpleNamespace(action_token_min=100, action_token_max=110)
    b.debug_tokenizer = SimpleNamespace(decode=lambda ids, **kw: "subtask")
    b.unnormalize = lambda data: data
    monkeypatch.setattr(b, "_prepare_inputs", lambda *args: inputs)

    actions = b.predict(np.zeros(14), {}, "task")

    assert actions.shape == (50, 14) and np.isfinite(actions).all()
    # The prompt FAST token must be stripped before generation, and an empty
    # generated subtask must not append tokens or keep a stale FAST mask.
    assert seen["input_ids"] == [[1, 2]]
    assert seen["attention_mask"] == [[1, 1]]
    assert not seen["fast_token_mask"].any()


def test_real_vendor_import_config_roundtrip_and_transforms(
    cpu_torch, monkeypatch, tmp_path
):
    torch = cpu_torch
    pytest.importorskip("transformers")
    monkeypatch.syspath_prepend(str(ROOT / "vendor/qwen35"))
    from lerobot.configs.policies import PreTrainedConfig
    from lerobot.dataset_schemas import get_schema
    from lerobot.policies.qwen3_5vla_ki import transform_qwen3_5vla_ki as chat
    from lerobot.policies.qwen3_5vla_ki_wan import (
        Qwen3_5VLAKIWanConfig,
        Qwen3_5VLAKIWanPolicy,
    )
    from lerobot.policies.qwen3_5vla_ki_wan.modeling_qwen3_5vla_ki_wan_opt import (
        Qwen3_5VLAKIWanFast,
    )
    from lerobot.transforms.core import (
        NormalizeTransformFn,
        RemapImageKeyTransformFn,
        ResizeImagesWithPadFn,
        UnNormalizeTransformFn,
        compose,
    )

    assert Qwen3_5VLAKIWanPolicy.config_class is Qwen3_5VLAKIWanConfig
    assert Qwen3_5VLAKIWanFast.__name__ == "Qwen3_5VLAKIWanFast"
    cfg = Qwen3_5VLAKIWanConfig(
        device="cpu", action_loss_only=True, inference_backend="optimized"
    )
    cfg.save_pretrained(tmp_path)
    loaded = PreTrainedConfig.from_pretrained(tmp_path, local_files_only=True)
    assert isinstance(loaded, Qwen3_5VLAKIWanConfig) and loaded.chunk_size == 50
    assert "lerobot.datasets.lerobot_dataset" not in sys.modules
    assert "lerobot.policies.pi0" not in sys.modules
    assert "lerobot.configs.train" not in sys.modules
    assert "lerobot.policies.qwen3_5vla_ki_wan.wan_model" not in sys.modules

    class Processor:
        def apply_chat_template(self, messages, **kw):
            assert kw["add_generation_prompt"]
            text = messages[1]["content"][-1]["text"]
            assert "Task: pick" in text and "Output: <Subtask, Action>" in text
            assert "State: " in text
            return "prompt"

        def __call__(self, **kw):
            assert kw["do_rescale"] is False and kw["padding"] is False
            assert len(kw["images"]) == 3
            assert kw["images"][0].shape == (3, 224, 224)
            return SimpleNamespace(
                input_ids=torch.tensor([[1, 2]]),
                attention_mask=torch.tensor([[1, 1]]),
                pixel_values=torch.ones(6, 4),
                image_grid_thw=torch.ones(3, 3, dtype=torch.long),
            )

    monkeypatch.setattr(
        chat.Qwen3_5KIChatProcessorTransformFn,
        "__post_init__",
        lambda self: setattr(self, "processor", Processor()),
    )
    b = make_backend()
    schema = get_schema("arx_acone")
    stats = {"observation.state": {"mean": np.zeros(14), "std": np.ones(14)}}
    b.input_transforms = compose(
        [
            ResizeImagesWithPadFn(height=224, width=224, mapping=schema.image_mapping),
            RemapImageKeyTransformFn(mapping=schema.image_mapping),
            NormalizeTransformFn(selected_keys=["observation.state"], norm_stats=stats),
            chat.Qwen3_5KIChatProcessorTransformFn(mode="eval"),
        ]
    )
    image = np.full((120, 160, 3), 127, np.uint8)
    result = b._prepare_inputs(
        np.zeros(14), {k: image for k in ("head", "left_wrist", "right_wrist")}, "pick"
    )
    assert result["observation.input_ids"].tolist() == [[1, 2]]
    assert result["observation.images.image0_mask"].tolist() == [True]
    transform = UnNormalizeTransformFn(
        selected_keys=["action"],
        norm_stats={"action": {"mean": np.full(14, 0.2), "std": np.full(14, 2.0)}},
    )
    got = transform({"action": torch.ones(50, 14)})["action"]
    torch.testing.assert_close(got, torch.full((50, 14), 2.200001))


def test_load_warmup_uses_checkpoint_config_and_rejects_wrong_chunk(
    cpu_torch, monkeypatch, tmp_path
):
    torch = cpu_torch
    pytest.importorskip("transformers")
    monkeypatch.syspath_prepend(str(ROOT / "vendor/qwen35"))
    from lerobot.configs.policies import PreTrainedConfig
    from lerobot.policies.qwen3_5vla_ki.transform_qwen3_5vla_ki import (
        Qwen3_5KIChatProcessorTransformFn,
    )
    from lerobot.policies.qwen3_5vla_ki_wan import (
        Qwen3_5VLAKIWanConfig,
        Qwen3_5VLAKIWanPolicy,
    )

    cfg = Qwen3_5VLAKIWanConfig(device="cpu")
    monkeypatch.setattr(PreTrainedConfig, "from_pretrained", lambda *a, **kw: cfg)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(torch.cuda, "synchronize", lambda: None)
    monkeypatch.setattr(
        Qwen3_5KIChatProcessorTransformFn, "__post_init__", lambda self: None
    )
    calls = []

    class Policy:
        def cuda(self):
            return self

        def to(self, dtype):
            assert dtype == torch.bfloat16
            return self

        def eval(self):
            return self

        def requires_grad_(self, value):
            assert value is False

        def predict_action_chunk(self, inputs):
            assert not torch.is_grad_enabled()
            assert inputs["observation.state"].shape == (1, 16)
            calls.append(inputs)

    def load_policy(**kw):
        assert kw["local_files_only"] is True and kw["config"] is cfg
        assert cfg.inference_backend == "optimized" and cfg.action_loss_only is True
        assert cfg.compile_model is False
        return Policy()

    monkeypatch.setattr(Qwen3_5VLAKIWanPolicy, "from_pretrained", load_policy)
    values = {"min": [0] * 14, "max": [1] * 14, "mean": [0] * 14, "std": [1] * 14}
    (tmp_path / "stats.json").write_text(
        json.dumps({"arx_acone": {"observation.state": values, "action": values}})
    )
    b = make_backend()
    b.checkpoint = tmp_path
    b.load()
    b.warmup()
    assert len(calls) == 2 and b.chunk_size == 50
    cfg.chunk_size = 49
    invalid = make_backend()
    invalid.checkpoint = tmp_path
    with pytest.raises(ValueError, match="chunk_size must be 50"):
        invalid.load()
