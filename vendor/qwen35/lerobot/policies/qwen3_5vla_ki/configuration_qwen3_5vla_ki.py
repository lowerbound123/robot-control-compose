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

# Adapted for inference-only robot-control deployment; see PROVENANCE.md.
from __future__ import annotations
from dataclasses import dataclass, field
from lerobot.configs.policies import PreTrainedConfig
from lerobot.configs.types import FeatureType, NormalizationMode, PolicyFeature
from lerobot.policies.qwen3_5vla_ki.action_tokens import ACTION_TOKEN_MAX, ACTION_TOKEN_MIN
from lerobot.utils.constants import OBS_IMAGES

@PreTrainedConfig.register_subclass("qwen3_5vla_ki")
@dataclass
class Qwen3_5VLAKIConfig(PreTrainedConfig):
    # VLM model selection - supports Qwen3.5-2B/4B/8B
    vlm_model_name_or_path: str = "Qwen/Qwen3.5-2B"

    # Action expert customization
    action_expert_hidden_size: int | None = 1024
    action_expert_intermediate_size: int | None = 3072

    dtype: str = "bfloat16"

    n_obs_steps: int = 1
    chunk_size: int = 50
    n_action_steps: int = 50

    max_state_dim: int = 32
    max_action_dim: int = 32

    # Flow matching parameters
    num_inference_steps: int = 10
    time_sampling_beta_alpha: float = 1.5
    time_sampling_beta_beta: float = 1.0
    time_sampling_scale: float = 0.999
    time_sampling_offset: float = 0.001
    min_period: float = 4e-3
    max_period: float = 4.0

    image_resolution: tuple[int, int] = (224, 224)
    empty_cameras: int = 0

    normalization_mapping: dict[str, NormalizationMode] = field(
        default_factory=lambda: {
            "VISUAL": NormalizationMode.IDENTITY,
            "STATE": NormalizationMode.IDENTITY,
            "ACTION": NormalizationMode.IDENTITY,
        }
    )

    # Training settings
    gradient_checkpointing: bool = False
    compile_model: bool = False
    compile_mode: str = "max-autotune"
    device: str | None = None

    # Optimizer settings
    optimizer_lr: float = 2.5e-5
    optimizer_betas: tuple[float, float] = (0.9, 0.95)
    optimizer_eps: float = 1e-8
    optimizer_weight_decay: float = 0.01
    optimizer_grad_clip_norm: float = 1.0

    scheduler_warmup_steps: int = 1_000
    scheduler_decay_steps: int = 30_000
    scheduler_decay_lr: float = 2.5e-6

    tokenizer_max_length: int = 48

    freeze_vision_encoder: bool = False
    train_expert_only: bool = False

    # VQA configurations
    enable_vqa_loss: bool = False
    lambda_vqa: float = 1.0
    tokenize_state: bool = False

    # FAST action tokens
    action_token_min: int = ACTION_TOKEN_MIN
    action_token_max: int = ACTION_TOKEN_MAX

    # Knowledge insulation
    knowledge_insulation: bool = True
    block_action_attend_fast_tokens: bool = True

    inference_action_type: str = "fm" # "fm" for flow matching, "fast" for fast-token supervision

    # Use SDPA (scaled_dot_product_attention) instead of eager attention in training
    use_sdpa: bool = False

    # Inference implementation selected by the public policy class.
    inference_backend: str = "standard"  # "standard" or "optimized"

    def __post_init__(self):
        super().__post_init__()

        if self.n_action_steps > self.chunk_size:
            raise ValueError(
                f"n_action_steps ({self.n_action_steps}) cannot be greater than chunk_size ({self.chunk_size})"
            )

        if self.dtype not in ["bfloat16", "float32"]:
            raise ValueError(f"Invalid dtype: {self.dtype}")
        if self.lambda_vqa < 0:
            raise ValueError(f"lambda_vqa must be >= 0, got {self.lambda_vqa}")
        if self.action_token_min > self.action_token_max:
            raise ValueError(
                f"action_token_min ({self.action_token_min}) must be <= action_token_max ({self.action_token_max})"
            )
        if (self.action_token_min, self.action_token_max) != (
            ACTION_TOKEN_MIN,
            ACTION_TOKEN_MAX,
        ):
            raise ValueError(
                "Qwen3.5 runtime action-token extension requires the fixed range "
                f"[{ACTION_TOKEN_MIN}, {ACTION_TOKEN_MAX}]"
            )
        if self.inference_backend not in {"standard", "optimized"}:
            raise ValueError(
                "inference_backend must be either 'standard' or 'optimized', "
                f"got {self.inference_backend!r}"
            )
    def validate_features(self) -> None:
        """Validate and set up input/output features."""
        for i in range(self.empty_cameras):
            key = f"{OBS_IMAGES}.empty_camera_{i}"
            empty_camera = PolicyFeature(
                type=FeatureType.VISUAL,
                shape=(3, *self.image_resolution),
            )
            self.input_features[key] = empty_camera

        if "observation.state" not in self.input_features:
            state_feature = PolicyFeature(
                type=FeatureType.STATE,
                shape=(self.max_state_dim,),
            )
            self.input_features["observation.state"] = state_feature

        if "action" not in self.output_features:
            action_feature = PolicyFeature(
                type=FeatureType.ACTION,
                shape=(self.max_action_dim,),
            )
            self.output_features["action"] = action_feature

    def get_optimizer_preset(self) -> AdamWConfig:
        raise NotImplementedError("Training is not included in this inference runtime")

    def get_scheduler_preset(self):
        raise NotImplementedError("Training is not included in this inference runtime")

    @property
    def observation_delta_indices(self) -> None:
        return None

    @property
    def action_delta_indices(self) -> list:
        return list(range(self.chunk_size))

    @property
    def reward_delta_indices(self) -> None:
        return None

