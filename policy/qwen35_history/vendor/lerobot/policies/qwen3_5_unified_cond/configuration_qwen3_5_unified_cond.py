#!/usr/bin/env python

# Copyright 2025 Physical Intelligence and The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from dataclasses import dataclass, field

from lerobot.configs.policies import PreTrainedConfig
from lerobot.configs.types import FeatureType, NormalizationMode, PolicyFeature
from lerobot.policies.qwen3_5_unified_cond.ibq_codec import IBQ_DOWNSAMPLE_FACTOR, IBQ_REPO_ID
from lerobot.policies.qwen3_5_unified_cond.tokenization_qwen3_5_unified_cond import (
    DEFAULT_FAST_TOKENIZER_NAME,
    FUTURE_VIEW_SAMPLING_HEAD_PLUS,
    FUTURE_VIEW_SAMPLING_MODES,
    INFERENCE_ORDER_ACTION_FIRST,
    INFERENCE_ORDER_BY_NAME,
    LOSS_SOFTMAX_DOMAINS,
    LOSS_SOFTMAX_UNIFIED,
    max_predicted_views,
)
from lerobot.utils.constants import OBS_IMAGES

# The cleaned sidecar (util_scripts/build_episode_language.py) wins over the raw annotation files.

# NormalizeTransformFn.hydrate forces selected_keys to the schema's state keys
# plus action keys, so one mode always covers state and action together.
NORM_MODES: tuple[str, ...] = ("mean_std", "min_max", "q01_q99")

# Field names Qwen3_5UnifiedCondDatasetConfig mirrors one-to-one from
# Qwen3_5UnifiedCondConfig. Both dataclasses must declare these with identical
# defaults; configure_for_policy copies them straight across. The policy's
# image_resolution -> (height, width) is the one field handled separately.
_POLICY_MIRRORED_FIELDS: tuple[str, ...] = (
    "chunk_size",
    "max_state_dim",
    "max_action_dim",
    "max_prompt_tokens",
    "max_subtask_tokens",
    "max_action_tokens",
    "future_first_probability",
    "inference_order",
    "generate_subtask",
    "future_image_indices",
    "cond_frame_enabled",
    "max_num_views",
    "future_view_sampling",
    "predict_future_images",
    "tokenize_state",
    "image_resize_mode",
    "image_resize_long_side",
    "ibq_image_resize_mode",
    "vlm_model_name_or_path",
    "action_tokenizer_name",
)




@PreTrainedConfig.register_subclass("qwen3_5_unified_cond")
@dataclass
class Qwen3_5UnifiedCondConfig(PreTrainedConfig):
    vlm_model_name_or_path: str = "Qwen/Qwen3.5-2B"
    action_tokenizer_name: str = DEFAULT_FAST_TOKENIZER_NAME
    ibq_model_name_or_path: str = IBQ_REPO_ID

    dtype: str = "bfloat16"
    attn_implementation: str = "flash_attention_2"
    chunk_size: int = 50
    n_action_steps: int = 50
    max_state_dim: int = 32
    max_action_dim: int = 32
    max_prompt_tokens: int = 350
    max_subtask_tokens: int = 64
    max_action_tokens: int = 256
    future_first_probability: float = 0.5
    inference_order: str = "action_first"
    generate_subtask: bool = True
    future_image_indices: tuple[int, ...] = (0,)
    image_resize_mode: str = "pad"
    image_resize_long_side: int = 256
    # Geometry of the IBQ cond/future frames, independent of the ViT input:
    # "long_side" reuses the frame image_resize_mode produced, zero-padded on the
    # bottom/right up to multiples of 16 (a no-op for already-divisible frames,
    # e.g. 16:9 / 4:3 at long side 256, so older checkpoints are unchanged); "pad"
    # pads it to image_resolution so every view has the same IBQ grid. A cosmetic
    # black border for 16:9.
    ibq_image_resize_mode: str = "long_side"

    # Conditioning frames: the current observation re-encoded with the frozen IBQ
    # codec and placed in the context, so keeping the background is token copying
    # instead of a ViT-feature-to-IBQ-code translation.
    cond_frame_enabled: bool = True
    # Sizes the MRoPE view axis, so inference may generate views training never
    # sampled together. Never the per-sample view count.
    max_num_views: int = 3
    ibq_grid_positions: bool = True
    # Which of the candidate views in future_image_indices one sample predicts.
    future_view_sampling: str = FUTURE_VIEW_SAMPLING_HEAD_PLUS
    # Action-only ablation: drop the cond/future IBQ blocks from the sequence so
    # the model only predicts FAST action tokens (plus optional subtask text).
    predict_future_images: bool = True
    # KI-style state input: replace the single <|learned_state|> projection token
    # with discretized state text ("State: 128 45 ..."), mirroring qwen3_5vla_ki.
    # Costs roughly max_state_dim extra prompt tokens; size max_prompt_tokens for it.
    tokenize_state: bool = False

    action_loss_weight: float = 1.0
    future_loss_weight: float = 1.0
    subtask_loss_weight: float = 1.0
    # Normalization domain and segment weighting stay independent variables:
    # "unified" (default) normalizes over the whole model vocabulary with
    # model-global labels, matching qwen3_5_fast's full-vocabulary CE;
    # "segment" normalizes inside each segment's vocabulary slice.
    loss_softmax_domain: str = LOSS_SOFTMAX_UNIFIED
    # Greedy decoding restricted to the segment's slice. Align with training.
    constrained_decoding: bool = True
    # "standard" runs generation through the stock HF decode loop; "optimized"
    # swaps in the inference-only fast backend (static KV cache, block prefill
    # of known tokens, CUDA-Graph decode steps). Batch size 1 only.
    inference_backend: str = "standard"

    image_resolution: tuple[int, int] = (256, 256)
    empty_cameras: int = 0
    gradient_checkpointing: bool = False
    compile_model: bool = False
    compile_mode: str = "max-autotune"
    freeze_vision_encoder: bool = False
    device: str | None = None

    optimizer_lr: float = 2.5e-5
    optimizer_betas: tuple[float, float] = (0.9, 0.95)
    optimizer_eps: float = 1e-8
    optimizer_weight_decay: float = 0.01
    optimizer_grad_clip_norm: float = 1.0
    scheduler_warmup_steps: int = 1_000
    scheduler_decay_steps: int = 30_000
    scheduler_decay_lr: float = 2.5e-6

    normalization_mapping: dict[str, NormalizationMode] = field(
        default_factory=lambda: {
            "VISUAL": NormalizationMode.IDENTITY,
            "STATE": NormalizationMode.IDENTITY,
            "ACTION": NormalizationMode.IDENTITY,
        }
    )

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.chunk_size != 50:
            raise ValueError(f"qwen3_5_unified_cond currently fixes H=50, got {self.chunk_size}")
        if self.image_resolution != (256, 256):
            raise ValueError("qwen3_5_unified_cond currently fixes image_resolution=(256, 256)")
        if not 0 < self.n_action_steps <= self.chunk_size:
            raise ValueError("n_action_steps must be in [1, chunk_size]")
        if self.dtype not in ("bfloat16", "float32"):
            raise ValueError(f"Unsupported dtype: {self.dtype}")
        if self.attn_implementation not in ("flash_attention_2", "sdpa", "eager"):
            raise ValueError("attn_implementation must be one of 'flash_attention_2', 'sdpa', or 'eager'")
        if not 0.0 <= self.future_first_probability <= 1.0:
            raise ValueError("future_first_probability must be in [0,1]")
        if self.inference_order not in INFERENCE_ORDER_BY_NAME:
            raise ValueError("inference_order must be either 'future_first' or 'action_first'")
        if not isinstance(self.generate_subtask, bool):
            raise TypeError("generate_subtask must be a boolean")
        if self.image_resize_mode not in ("pad", "long_side"):
            raise ValueError("image_resize_mode must be either 'pad' or 'long_side'")
        if self.image_resize_long_side <= 0:
            raise ValueError("image_resize_long_side must be positive")
        if self.ibq_image_resize_mode not in ("pad", "long_side"):
            raise ValueError("ibq_image_resize_mode must be either 'pad' or 'long_side'")
        if not self.future_image_indices or len(set(self.future_image_indices)) != len(
            self.future_image_indices
        ):
            raise ValueError("future_image_indices must contain unique camera indices")
        if any(index not in range(self.max_num_views) for index in self.future_image_indices):
            raise ValueError(f"future_image_indices must be in [0, {self.max_num_views})")
        if self.future_view_sampling not in FUTURE_VIEW_SAMPLING_MODES:
            raise ValueError(f"future_view_sampling must be one of {FUTURE_VIEW_SAMPLING_MODES}")
        if not self.predict_future_images and (
            self.future_first_probability != 0.0 or self.inference_order != INFERENCE_ORDER_ACTION_FIRST
        ):
            raise ValueError(
                "predict_future_images=False requires future_first_probability=0.0 and "
                "inference_order='action_first'"
            )
        if self.loss_softmax_domain not in LOSS_SOFTMAX_DOMAINS:
            raise ValueError(f"loss_softmax_domain must be one of {LOSS_SOFTMAX_DOMAINS}")
        for name in ("action_loss_weight", "future_loss_weight", "subtask_loss_weight"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.inference_backend not in ("standard", "optimized"):
            raise ValueError(
                f"inference_backend must be 'standard' or 'optimized', got {self.inference_backend!r}"
            )
        if self.inference_backend == "optimized" and self.compile_model:
            raise ValueError("inference_backend='optimized' is incompatible with compile_model=True")

    def validate_features(self) -> None:
        for index in range(self.empty_cameras):
            key = f"{OBS_IMAGES}.empty_camera_{index}"
            self.input_features[key] = PolicyFeature(
                type=FeatureType.VISUAL, shape=(3, *self.image_resolution)
            )
        if "observation.state" not in self.input_features:
            self.input_features["observation.state"] = PolicyFeature(
                type=FeatureType.STATE, shape=(self.max_state_dim,)
            )
        if "action" not in self.output_features:
            self.output_features["action"] = PolicyFeature(
                type=FeatureType.ACTION, shape=(self.max_action_dim,)
            )

    def get_optimizer_preset(self):
        raise NotImplementedError("inference-only deployment")

    def get_scheduler_preset(self):
        raise NotImplementedError("inference-only deployment")

    @property
    def max_predicted_views(self) -> int:
        return max_predicted_views(self.future_image_indices, mode=self.future_view_sampling)

    @property
    def observation_delta_indices(self) -> None:
        return None

    @property
    def action_delta_indices(self) -> list[int]:
        return list(range(self.chunk_size))

    @property
    def reward_delta_indices(self) -> None:
        return None

    @property
    def image_delta_indices(self) -> list[int]:
        # Default for all cameras: current observation only.
        return [0]

    @property
    def image_delta_indices_by_key(self) -> dict[str, list[int]]:
        return {f"{OBS_IMAGES}.image{index}": [0, self.chunk_size] for index in self.future_image_indices}
