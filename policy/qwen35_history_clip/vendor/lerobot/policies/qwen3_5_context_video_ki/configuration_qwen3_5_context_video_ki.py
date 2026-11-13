#!/usr/bin/env python

"""Configuration for the video-context policy with a knowledge-insulated action expert.

Everything about the prefix -- context clips, markers, sampling, FAST tokens --
is inherited unchanged from ``qwen3_5_context_video``.  The fields added here
describe only the action expert and its flow-matching objective, and they use
``qwen3_5vla_ki``'s names and defaults so the two KI policies read the same.

The dataset config needs no new fields: the expert regresses the continuous
action chunk the base transform already normalizes and pads, so the only
dataset-side change is exporting it (see ``transform_qwen3_5_context_video_ki``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from lerobot.configs.policies import PreTrainedConfig
from lerobot.policies.qwen3_5_context_video.configuration_qwen3_5_context_video import (
    Qwen3_5ContextVideoConfig,
)

# Top-level import for the registration side effect: decoding a *saved*
# train config (resume) resolves the ``qwen3_5_context_video_ki_processor``
# choice in ``dataset.data_transforms.inputs`` before ``__post_init__`` runs,
# so the subclass must already be registered when this module (imported for
# the dataset-choice itself) loads.  unified_cond's configuration does the
# same for its processor.

INFERENCE_ACTION_TYPES = ("fm", "fast")
OPTIMIZER_TYPES = ("adamw", "muon", "muon_vl_adamw_action")


@dataclass
class _ContextVideoKIFieldsMixin:
    """KI geometry and training options; expert defaults follow ``Qwen3_5VLAKIConfig``."""

    # Action expert geometry.  Only these two are free: head_dim, head counts,
    # layer count, layer types and RoPE are copied from the VLM so the expert's
    # layer i can read the VLM's layer i K/V.
    action_expert_hidden_size: int | None = 1024
    action_expert_intermediate_size: int | None = 3072

    # Flow matching.
    # Independent (noise, time) draws per observed action chunk during training.
    # The VLM prefix and CE are evaluated once; the expert batch is B * K.
    num_flow_matching_samples: int = 4
    num_inference_steps: int = 10
    time_sampling_beta_alpha: float = 1.5
    time_sampling_beta_beta: float = 1.0
    time_sampling_scale: float = 0.999
    time_sampling_offset: float = 0.001
    min_period: float = 4e-3
    max_period: float = 4.0

    # Knowledge insulation: the expert reads the VLM's per-layer K/V detached,
    # so the flow-matching loss never reaches the backbone; only the FAST
    # cross-entropy shapes it.
    knowledge_insulation: bool = False
    # The expert may not attend to the FAST action segment (or anything after
    # it); it sees exactly the tokens the inference prefix contains.
    block_action_attend_fast_tokens: bool = True
    # The joint prefix/suffix layer loop runs its own attention calls;
    # SDPA selects a compatible backend, eager is the reference.
    use_sdpa: bool = True
    # Omit the dense prefix mask when the training prefix is causal with right
    # padding. This permits fused SDPA; the expert keeps its visibility mask.
    # Padding outputs may differ, while all valid outputs/losses are preserved.
    use_causal_prefix_sdpa: bool = False
    # Opt-in masked suffix SDPA with explicit KV heads and aligned mask storage.
    # Preserves the attention formula/dtypes; fused CUDA reductions may round differently.
    use_efficient_suffix_sdpa: bool = False
    # Read compatibility for previously exported policy configs only. These
    # experimental paths are not implemented here; True is rejected below.
    use_bf16_joint_hidden: bool = False
    use_compiled_pointwise: bool = False
    inference_action_type: str = "fm"
    # vla_ki hardcodes ``10 * loss_action + lambda_vqa * loss_vqa``; the same
    # ratio here, but configurable.  The FAST CE keeps its unified_cond weights.
    fm_loss_weight: float = 10.0
    # Mixed pretraining: weight of the VQA answer CE (``LOSS_VQA``, see
    # ``Qwen3_5VQAContextVideoKIDatasetConfig``); the segment is empty on robot rows.
    vqa_loss_weight: float = 1.0
    # Freeze the whole VLM (vision + language) and train only the expert.
    train_expert_only: bool = False
    # Muon covers transformer matrices; AdamW covers the remaining parameters.
    optimizer_type: str = "adamw"
    # Applies only to the AdamW preset. None preserves PyTorch's automatic
    # kernel selection; True requests its fused implementation.
    optimizer_fused: bool | None = None
    muon_lr: float | None = None
    muon_weight_decay: float | None = None
    muon_momentum: float = 0.95
    muon_nesterov: bool = True
    muon_ns_steps: int = 5
    muon_adjust_lr_fn: str = "match_rms_adamw"

    # Used only when the inherited gradient_checkpointing switch is enabled.
    # Defaults preserve checkpointing of all vision and joint decoder layers.
    gradient_checkpointing_vision: bool = True
    gradient_checkpointing_decoder: bool = True
    # Checkpoint every Nth joint layer (zero-based); 1 checkpoints all layers.
    gradient_checkpointing_decoder_stride: int = 1
    # N>1 skips every Nth layer (one-based); 0 disables this alternative schedule.
    gradient_checkpointing_decoder_skip_stride: int = 0
    # Skip the first N full-attention layers; checkpoint every linear layer.
    gradient_checkpointing_decoder_uncheckpointed_full_layers: int = 0


def _validate_context_video_ki_config(config: _ContextVideoKIFieldsMixin) -> None:
    for name in (
        "use_efficient_suffix_sdpa",
        "use_bf16_joint_hidden",
        "use_compiled_pointwise",
        "gradient_checkpointing_vision",
        "gradient_checkpointing_decoder",
    ):
        if not isinstance(getattr(config, name), bool):
            raise ValueError(f"{name} must be a bool")
    for name in ("use_bf16_joint_hidden", "use_compiled_pointwise"):
        if getattr(config, name):
            raise ValueError(
                f"{name} is deprecated and unsupported; legacy checkpoint loading requires false"
            )
    for name in (
        "gradient_checkpointing_decoder_stride",
        "gradient_checkpointing_decoder_skip_stride",
        "gradient_checkpointing_decoder_uncheckpointed_full_layers",
    ):
        if type(getattr(config, name)) is not int:
            raise ValueError(f"{name} must be an integer")
    for name in ("action_expert_hidden_size", "action_expert_intermediate_size"):
        value = getattr(config, name)
        if value is not None and value <= 0:
            raise ValueError(f"{name} must be positive when set")
    if type(config.num_flow_matching_samples) is not int or config.num_flow_matching_samples < 1:
        raise ValueError("num_flow_matching_samples must be a positive integer")
    if config.num_inference_steps <= 0:
        raise ValueError("num_inference_steps must be positive")
    if config.fm_loss_weight < 0:
        raise ValueError("fm_loss_weight must be non-negative")
    if config.vqa_loss_weight < 0:
        raise ValueError("vqa_loss_weight must be non-negative")
    if config.inference_action_type not in INFERENCE_ACTION_TYPES:
        raise ValueError(f"inference_action_type must be one of {INFERENCE_ACTION_TYPES}")
    if config.optimizer_type not in OPTIMIZER_TYPES:
        raise ValueError(f"optimizer_type must be one of {OPTIMIZER_TYPES}, got {config.optimizer_type!r}")
    if config.optimizer_fused is not None and not isinstance(config.optimizer_fused, bool):
        raise ValueError("optimizer_fused must be a bool or None")
    if config.muon_lr is not None and config.muon_lr <= 0:
        raise ValueError("muon_lr must be positive when set")
    if config.muon_weight_decay is not None and config.muon_weight_decay < 0:
        raise ValueError("muon_weight_decay must be non-negative")
    if not 0 <= config.muon_momentum < 1:
        raise ValueError("muon_momentum must be in [0, 1)")
    if config.muon_ns_steps <= 0:
        raise ValueError("muon_ns_steps must be positive")
    if config.muon_adjust_lr_fn not in ("match_rms_adamw", "original"):
        raise ValueError("muon_adjust_lr_fn must be match_rms_adamw or original")
    if config.gradient_checkpointing_decoder_stride < 1:
        raise ValueError("gradient_checkpointing_decoder_stride must be at least 1")
    if (
        config.gradient_checkpointing_decoder_skip_stride != 0
        and config.gradient_checkpointing_decoder_skip_stride < 2
    ):
        raise ValueError("gradient_checkpointing_decoder_skip_stride must be 0 or at least 2")
    if config.gradient_checkpointing_decoder_uncheckpointed_full_layers < 0:
        raise ValueError("gradient_checkpointing_decoder_uncheckpointed_full_layers must be non-negative")
    if (
        config.gradient_checkpointing_decoder_skip_stride > 0
        and config.gradient_checkpointing_decoder_stride != 1
    ):
        raise ValueError("decoder_stride must be 1 when decoder_skip_stride is enabled")
    if config.gradient_checkpointing_decoder_uncheckpointed_full_layers > 0 and (
        config.gradient_checkpointing_decoder_stride != 1
        or config.gradient_checkpointing_decoder_skip_stride != 0
    ):
        raise ValueError(
            "decoder stride/skip must stay at 1/0 when selecting uncheckpointed full-attention layers"
        )
    # generate_subtask=True trains the language segment before the FAST action
    # boundary. Inference fields are selected by build_inference's language /
    # generate request, including externally supplied and model-generated fields.
    if not config.predict_future_images and (
        config.future_first_probability != 0.0 or config.inference_order != "action_first"
    ):
        raise ValueError(
            "predict_future_images=False requires future_first_probability=0 and inference_order='action_first'"
        )
    if config.inference_backend != "standard":
        raise ValueError("qwen3_5_context_video_ki has no optimized backend; inference_backend must be 'standard'")






@PreTrainedConfig.register_subclass("qwen3_5_context_video_ki")
@dataclass
class Qwen3_5ContextVideoKIConfig(  # noqa: N801
    _ContextVideoKIFieldsMixin, Qwen3_5ContextVideoConfig
):
    def __post_init__(self) -> None:
        super().__post_init__()
        _validate_context_video_ki_config(self)

    def get_optimizer_preset(self):
        raise NotImplementedError("inference-only deployment")

    def get_scheduler_preset(self):
        raise NotImplementedError("inference-only deployment")
