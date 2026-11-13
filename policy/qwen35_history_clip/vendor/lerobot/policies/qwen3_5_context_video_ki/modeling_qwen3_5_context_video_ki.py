#!/usr/bin/env python

"""qwen3_5_context_video_ki model: the context_video backbone plus a knowledge-insulated action expert.

Self-contained on purpose: every code path this policy can reach is written out
here rather than inherited, so the model can be read and modified top to
bottom.  The pieces and where they come from:

* token table, prompt embedding, MRoPE positions (incl. the IBQ grid planes),
  online IBQ encoding of cond/future frames, FAST and future-image
  cross-entropy, semantic prefix extraction, the greedy subtask stage, the
  cond/future image stage and the greedy FAST decode -- ``qwen3_5_unified_cond``,
  with one ``<|learned_state|>`` token.  The subtask is generated against a
  throwaway prefill and the prompt re-prefilled with the segment appended
  (``_generate_subtask_ids``) instead of unified_cond's one-token-at-a-time FSM;
* the sparse context-marker ids and their input embeddings --
  ``qwen3_5_context_video``;
* the action expert, the suffix embedding, the joint prefix/suffix layer loop
  with detached prefix K/V and the flow-matching objective -- ``qwen3_5vla_ki``.

Two invariants keep training and deployment aligned.  Both follow one rule:
the expert sees everything before ``<|unified_action_start|>`` and nothing
from it on.

* the suffix may attend only to that region -- the teacher-forced FAST segment
  is masked out.  In action-first order (``<Action, Cond, Future>``) that is
  the semantic prefix alone; in future-first order (``<Cond, Future, Action>``)
  it also holds the conditioning frames and the (teacher-forced) future images;
* the suffix positions continue from the last visible token, not from the end
  of the padded training sequence.

At inference the VLM is prefilled once (its ordinary KV cache); in future-first
order the cond blocks ride along in that prefill and the future bodies are then
decoded autoregressively into the same cache.  The expert alone runs
``num_inference_steps`` Euler steps against that cache.  The expert
reads the cache without updating it, which needs the ``use_cache=False`` branch
that ``transformers_replace/.../modeling_qwen3_5.py`` (qwen3_5vla / qwen3_5vla_ki)
adds to ``Qwen3_5Attention.forward``.
"""

from __future__ import annotations

import contextlib
import math
from collections import deque
from dataclasses import fields
from typing import Any

import torch
import torch.nn.functional as F  # noqa: N812
from torch import Tensor, nn
from transformers.models.auto import CONFIG_MAPPING
from transformers.models.qwen3_5 import (
    Qwen3_5ForConditionalGeneration,
    Qwen3_5TextModel,
    Qwen3_5Tokenizer,
    modeling_qwen3_5,
)
from transformers.utils import logging as transformers_logging
from typing_extensions import Unpack

from lerobot.policies.pretrained import ActionSelectKwargs, PreTrainedPolicy
from lerobot.policies.qwen3_5_context_video.tokenization_qwen3_5_context_video import (
    CONTEXT_VIDEO_TOKEN_SCHEMA_VERSION,
    CTX_TOKEN_START,
    CTX_VIDEO_TOKEN_END,
    LANGUAGE_GENERATE_KEY,
    LANGUAGE_IDS_KEY,
    LANGUAGE_LENGTH_KEY,
)
from lerobot.policies.qwen3_5_context_video_ki.configuration_qwen3_5_context_video_ki import (
    Qwen3_5ContextVideoKIConfig,
)
from lerobot.policies.qwen3_5_unified_cond.video_keys import (
    PIXEL_VALUES_VIDEOS_KEY,
    VIDEO_GRID_THW_KEY,
)
from lerobot.policies.qwen3_5_unified_cond.tokenization_qwen3_5_unified_cond import (
    CONTROL_TOKEN_END,
    CONTROL_TOKEN_START,
    FUTURE_TOKEN_PLACEHOLDER,
    IBQ_KIND_COND,
    IBQ_KIND_FUTURE,
    IBQ_KIND_NONE,
    IBQ_TOKEN_END,
    IBQ_TOKEN_OFFSET,
    IBQ_TOKEN_START,
    LOSS_ACTION,
    LOSS_FUTURE,
    LOSS_NONE,
    LOSS_SOFTMAX_UNIFIED,
    LOSS_SUBTASK,
    LOSS_VQA,
    MODEL_VOCAB_SIZE,
    NUM_ACTION_TOKENS,
    ORDER_ACTION_FIRST,
    ORDER_FUTURE_FIRST,
    QWEN_TEXT_VOCAB_SIZE,
    UnifiedSpecialTokenIds,
    ensure_unified_special_tokens,
    global_ids_to_ibq_code_ids,
    ibq_code_ids_to_global_ids,
    order_from_name,
    resolve_local_snapshot,
)
from lerobot.policies.qwen3_5_unified_cond.transform_qwen3_5_unified_cond import (
    COND_CACHED_KEY,
    FUTURE_GRID_HW_KEY,
    FUTURE_VIEW_INDICES_KEY,
    IBQ_COORDS_KEY,
    IBQ_FRAME_COUNT_KEY,
    IBQ_FRAME_META_KEY,
    IBQ_FRAMES_KEY,
    IBQ_KIND_KEY,
    unpack_ibq_frames,
)
from lerobot.policies.qwen3_5vla_ki.action_tokens import ACTION_TOKEN_MAX, ACTION_TOKEN_MIN
from lerobot.policies.qwen3_5_unified_cond.ibq_codec import IBQ_DOWNSAMPLE_FACTOR
from lerobot.utils.constants import ACTION, OBS_STATE, OBS_STR, OPENPI_ATTENTION_MASK_VALUE
from lerobot.utils.utils import format_big_number

# ---------------------------------------------------------------------------
# Token table (from qwen3_5_unified_cond, action-only)
# ---------------------------------------------------------------------------


def _load_or_validate_dependencies(
    config: Qwen3_5ContextVideoKIConfig,
    *,
    qwen: nn.Module | None,
    tokenizer: Any | None,
    special_tokens: UnifiedSpecialTokenIds | None,
) -> tuple[nn.Module, Any | None, UnifiedSpecialTokenIds]:
    dependencies_injected = qwen is not None or tokenizer is not None or special_tokens is not None
    if dependencies_injected and (qwen is None or special_tokens is None):
        raise ValueError(
            "Tests may inject unified dependencies only as qwen + special_tokens; "
            "tokenizer is optional when the injected table is already prepared"
        )

    if not dependencies_injected:
        snapshot = resolve_local_snapshot(config.vlm_model_name_or_path)
        tokenizer = Qwen3_5Tokenizer.from_pretrained(str(snapshot), local_files_only=True)
        special_tokens = ensure_unified_special_tokens(tokenizer)
        progress_was_enabled = transformers_logging.is_progress_bar_enabled()
        transformers_logging.disable_progress_bar()
        try:
            qwen = Qwen3_5ForConditionalGeneration.from_pretrained(
                str(snapshot),
                dtype=getattr(torch, config.dtype),
                attn_implementation=config.attn_implementation,
                local_files_only=True,
            )
        finally:
            if progress_was_enabled:
                transformers_logging.enable_progress_bar()
    elif tokenizer is not None:
        derived_special_tokens = ensure_unified_special_tokens(tokenizer)
        if derived_special_tokens != special_tokens:
            raise ValueError("Injected special_tokens do not match the injected tokenizer")

    expected_controls = list(range(CONTROL_TOKEN_START, CONTROL_TOKEN_END))
    actual_controls = [int(getattr(special_tokens, item.name)) for item in fields(special_tokens)]
    if actual_controls != expected_controls:
        raise ValueError(f"Unified control token ids must be {expected_controls}, got {actual_controls}")
    return qwen, tokenizer, special_tokens


def _synchronize_vocab_sizes(qwen: nn.Module, vocab_size: int) -> None:
    """Keep every Transformers config layer consistent with the physical table."""

    configurations = [getattr(qwen, "config", None)]
    outer_config = configurations[0]
    if outer_config is not None:
        configurations.append(getattr(outer_config, "text_config", None))
    model = getattr(qwen, "model", None)
    language_model = getattr(model, "language_model", None)
    configurations.append(getattr(language_model, "config", None))
    for configuration in configurations:
        if configuration is not None:
            configuration.vocab_size = vocab_size


def _prepare_unified_token_table(qwen: nn.Module, vocab_size: int) -> None:
    """Resize once, initialize the FAST/control (and IBQ) rows, and enforce tied input/output rows.

    ``vocab_size`` is ``MODEL_VOCAB_SIZE`` with future images (text + FAST +
    control + IBQ) or ``IBQ_TOKEN_START`` without: the action-only layout never
    touches IBQ ids, so its table stops at the control tokens.
    """

    if vocab_size < IBQ_TOKEN_START:
        raise ValueError(f"vocab_size must cover the control tokens, got {vocab_size}")
    input_embeddings = qwen.get_input_embeddings()
    old_size = int(input_embeddings.weight.shape[0])
    hidden_size = int(input_embeddings.weight.shape[1])
    if old_size < QWEN_TEXT_VOCAB_SIZE:
        raise ValueError(
            f"Qwen input table has {old_size} rows; expected at least "
            f"{QWEN_TEXT_VOCAB_SIZE} pretrained text rows"
        )
    resized = old_size != vocab_size
    if resized:
        resize = getattr(qwen, "resize_token_embeddings", None)
        if resize is None:
            raise ValueError(f"Qwen table has {old_size} rows and does not expose resize_token_embeddings")
        resize(vocab_size, mean_resizing=False)

    tie_weights = getattr(qwen, "tie_weights", None)
    if tie_weights is not None:
        tie_weights()

    input_embeddings = qwen.get_input_embeddings()
    output_embeddings = qwen.get_output_embeddings()
    if output_embeddings is None:
        raise ValueError("Qwen must expose an output token table")
    expected_shape = (vocab_size, hidden_size)
    if tuple(input_embeddings.weight.shape) != expected_shape:
        raise ValueError(
            f"Qwen input table must have shape {expected_shape}, got {tuple(input_embeddings.weight.shape)}"
        )
    if tuple(output_embeddings.weight.shape) != expected_shape:
        raise ValueError(
            f"Qwen output table must have shape {expected_shape}, got {tuple(output_embeddings.weight.shape)}"
        )
    if output_embeddings.weight.data_ptr() != input_embeddings.weight.data_ptr():
        # Some small injected stand-ins do not implement tie_weights. Binding
        # the Parameter explicitly mirrors Transformers' tied-head contract.
        output_embeddings.weight = input_embeddings.weight
    if qwen.get_output_embeddings().weight.data_ptr() != input_embeddings.weight.data_ptr():
        raise ValueError("qwen3_5_context_video_ki requires tied Qwen input/output weights")

    if resized:
        initializer_range = float(getattr(getattr(qwen.config, "text_config", None), "initializer_range", 0.02))
        with torch.no_grad():
            weight = input_embeddings.weight
            reference = weight[:QWEN_TEXT_VOCAB_SIZE].mean(dim=0, keepdim=True)
            weight[ACTION_TOKEN_MIN:IBQ_TOKEN_START].copy_(reference.expand(IBQ_TOKEN_START - ACTION_TOKEN_MIN, -1))
            if vocab_size > IBQ_TOKEN_START:
                nn.init.normal_(weight[IBQ_TOKEN_START:IBQ_TOKEN_END], mean=0.0, std=initializer_range)
    _synchronize_vocab_sizes(qwen, vocab_size)


def _flat_video_grid(video_grid_thw: Any) -> Tensor | None:
    """Normalize an optional video grid to ``[N,3]``.

    Samples without context still carry the key so the collate key set stays
    fixed across a batch; an empty grid is therefore the same as no video at all.
    """
    if video_grid_thw is None:
        return None
    grid = torch.as_tensor(video_grid_thw).reshape(-1, 3)
    return grid.long() if grid.numel() else None








# ---------------------------------------------------------------------------
# Knowledge-insulation kernel (from qwen3_5vla_ki)
# ---------------------------------------------------------------------------


def _with_prefix_outputs(
    result: Any, future_tokens: Any, subtask_ids: Any, return_future_tokens: bool, return_subtask_ids: bool
) -> Any:
    """``result`` alone, or the tuple the ``return_*`` flags of the inference entry points ask for."""
    outputs = (result,)
    if return_future_tokens:
        outputs += (future_tokens,)
    if return_subtask_ids:
        outputs += (subtask_ids,)
    return outputs[0] if len(outputs) == 1 else outputs


def _inference_autocast(module: nn.Module):
    """bf16 autocast for the inference passes on CUDA; a no-op elsewhere, so the CPU tests stay fp32.

    Training keeps fp32 master weights and computes under bf16 autocast (accelerate
    ``--mixed_precision=bf16``); wrapping inference the same way runs the bf16
    matmuls the model was trained with instead of a full-fp32 pass.
    """
    device = next(module.parameters()).device
    if device.type != "cuda":
        return contextlib.nullcontext()
    return torch.autocast(device_type="cuda", dtype=torch.bfloat16)


def _fp32_head(head: nn.Linear, hidden: Tensor) -> Tensor:
    """Apply the velocity head outside autocast: its output is the action, so it keeps the weights' precision."""
    with torch.autocast(device_type=hidden.device.type, enabled=False):
        return head(hidden.to(head.weight.dtype)).float()


def get_safe_dtype(target_dtype, device_type):
    """Get a safe dtype for the given device type."""
    if device_type == "mps" and target_dtype == torch.float64:
        return torch.float32
    if device_type == "cpu":
        if target_dtype == torch.bfloat16:
            return torch.float32
        if target_dtype == torch.float64:
            return torch.float64
    return target_dtype


def create_sinusoidal_pos_embedding(
    time: torch.Tensor, dimension: int, min_period: float, max_period: float, device="cpu"
) -> Tensor:
    """Computes sine-cosine positional embedding vectors for scalar positions."""
    if dimension % 2 != 0:
        raise ValueError(f"dimension ({dimension}) must be divisible by 2")

    if time.ndim != 1:
        raise ValueError("The time tensor is expected to be of shape `(batch_size, )`.")

    dtype = get_safe_dtype(torch.float64, device.type)
    fraction = torch.linspace(0.0, 1.0, dimension // 2, dtype=dtype, device=device)
    period = min_period * (max_period / min_period) ** fraction

    scaling_factor = 1.0 / period * 2 * math.pi
    sin_input = scaling_factor[None, :] * time[:, None]
    return torch.cat([torch.sin(sin_input), torch.cos(sin_input)], dim=1)




def make_att_2d_masks(pad_masks, att_masks):
    """Copied from big_vision."""
    if att_masks.ndim != 2:
        raise ValueError(att_masks.ndim)
    if pad_masks.ndim != 2:
        raise ValueError(pad_masks.ndim)

    cumsum = torch.cumsum(att_masks, dim=1)
    att_2d_masks = cumsum[:, None, :] <= cumsum[:, :, None]
    pad_2d_masks = pad_masks[:, None, :] * pad_masks[:, :, None]
    return att_2d_masks & pad_2d_masks






def _build_action_expert(text_config, *, hidden_size: int | None, intermediate_size: int | None) -> Qwen3_5TextModel:
    """A second Qwen3.5 text stack mirroring the VLM's layer geometry (vla_ki's recipe).

    Only ``hidden_size`` / ``intermediate_size`` are free: head_dim, head counts,
    layer count, layer types and RoPE must match so the expert's layer i can
    attend to the VLM's layer i K/V.  Randomly initialized, no token table.
    """
    expert_config = CONFIG_MAPPING["qwen3_5_text"]()
    expert_config.head_dim = text_config.head_dim
    expert_config.hidden_size = text_config.hidden_size if hidden_size is None else hidden_size
    expert_config.intermediate_size = (
        text_config.intermediate_size if intermediate_size is None else intermediate_size
    )
    expert_config.num_attention_heads = text_config.num_attention_heads
    expert_config.num_key_value_heads = text_config.num_key_value_heads
    expert_config.num_hidden_layers = text_config.num_hidden_layers
    expert_config.max_position_embeddings = text_config.max_position_embeddings
    expert_config.rope_parameters = text_config.rope_parameters
    expert_config.rms_norm_eps = text_config.rms_norm_eps
    expert_config.layer_types = text_config.layer_types
    expert_config.linear_conv_kernel_dim = text_config.linear_conv_kernel_dim
    expert_config.linear_key_head_dim = text_config.linear_key_head_dim
    expert_config.linear_value_head_dim = text_config.linear_value_head_dim
    expert_config.linear_num_key_heads = text_config.linear_num_key_heads
    expert_config.linear_num_value_heads = text_config.linear_num_value_heads
    # The denoise step hands the expert a pre-built 4-D additive mask; only the
    # eager path takes it verbatim (vla_ki sets this before every call).
    expert_config._attn_implementation = "eager"
    expert = Qwen3_5TextModel(config=expert_config)
    expert.embed_tokens = None
    return expert


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class Qwen3_5ContextVideoKIModel(nn.Module):  # noqa: N801
    """Context-video Qwen backbone (FAST head) plus a knowledge-insulated flow-matching expert."""

    token_schema_version = CONTEXT_VIDEO_TOKEN_SCHEMA_VERSION

    def __init__(
        self,
        config: Qwen3_5ContextVideoKIConfig,
        *,
        qwen: nn.Module | None = None,
        tokenizer: Any | None = None,
        special_tokens: UnifiedSpecialTokenIds | None = None,
        image_codec: Any | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self.register_buffer(
            "_unified_token_schema_version",
            torch.tensor(self.token_schema_version, dtype=torch.int64),
            persistent=True,
        )
        qwen, tokenizer, special_tokens = _load_or_validate_dependencies(
            config, qwen=qwen, tokenizer=tokenizer, special_tokens=special_tokens
        )
        self.qwen = qwen
        self.special_tokens = special_tokens
        # Without future images the table stops at the control tokens (unified_cond's
        # action-only ablation); with them it carries the 16K IBQ rows.
        self.vocab_size = MODEL_VOCAB_SIZE if config.predict_future_images else IBQ_TOKEN_START
        _prepare_unified_token_table(self.qwen, self.vocab_size)

        qwen_weight = self.qwen.get_input_embeddings().weight
        self.hidden_size = int(qwen_weight.shape[1])
        initializer_range = float(
            getattr(getattr(self.qwen.config, "text_config", None), "initializer_range", 0.02)
        )
        # The one <|learned_state|> token in the prefix.
        self.state_projection = nn.Linear(
            config.max_state_dim, self.hidden_size, bias=True, device=qwen_weight.device, dtype=qwen_weight.dtype
        )
        nn.init.normal_(self.state_projection.weight, mean=0.0, std=initializer_range)
        nn.init.zeros_(self.state_projection.bias)

        # Sparse context-marker ids live above the Qwen/IBQ range and get their
        # own embeddings, initialized to the mean pretrained text row.
        self.context_marker_embeddings = nn.Embedding(
            CTX_VIDEO_TOKEN_END - CTX_TOKEN_START, self.hidden_size, device=qwen_weight.device, dtype=qwen_weight.dtype
        )
        with torch.no_grad():
            reference = qwen_weight[:QWEN_TEXT_VOCAB_SIZE].mean(dim=0, keepdim=True)
            self.context_marker_embeddings.weight.copy_(reference.expand_as(self.context_marker_embeddings.weight))

        # Action expert in the backbone dtype; the small projections stay
        # float32 (vla_ki): the suffix is cast to the backbone dtype before the
        # joint pass and back to float32 before the velocity head.
        self.action_expert = _build_action_expert(
            self.qwen.config.text_config,
            hidden_size=config.action_expert_hidden_size,
            intermediate_size=config.action_expert_intermediate_size,
        )
        self.action_expert.to(device=qwen_weight.device, dtype=qwen_weight.dtype)
        expert_hidden_size = self.action_expert.config.hidden_size
        self.state_proj = nn.Linear(config.max_state_dim, expert_hidden_size, device=qwen_weight.device)
        self.action_in_proj = nn.Linear(config.max_action_dim, expert_hidden_size, device=qwen_weight.device)
        self.action_out_proj = nn.Linear(expert_hidden_size, config.max_action_dim, device=qwen_weight.device)
        self.action_time_mlp_in = nn.Linear(2 * expert_hidden_size, expert_hidden_size, device=qwen_weight.device)
        self.action_time_mlp_out = nn.Linear(expert_hidden_size, expert_hidden_size, device=qwen_weight.device)

        # Frozen codec remains deliberately outside nn.Module/state_dict.
        self.__dict__["_image_codec"] = image_codec
        self.__dict__["_tokenizer"] = tokenizer
        self._apply_runtime_options(config)

    def _apply_runtime_options(self, config: Qwen3_5ContextVideoKIConfig) -> None:
        if config.freeze_vision_encoder:
            self.qwen.visual.requires_grad_(False)
            self.qwen.visual.eval()
        if config.train_expert_only:
            # Everything the prefix branch owns, so no backward runs through the frozen stack.
            for module in (self.qwen, self.state_projection, self.context_marker_embeddings):
                module.requires_grad_(False)
            self.qwen.eval()
        if config.gradient_checkpointing:
            available_full_layers = sum(
                layer.layer_type == "full_attention" for layer in self.qwen.language_model.layers
            )
            if config.gradient_checkpointing_decoder_uncheckpointed_full_layers > available_full_layers:
                raise ValueError(
                    "gradient_checkpointing_decoder_uncheckpointed_full_layers="
                    f"{config.gradient_checkpointing_decoder_uncheckpointed_full_layers} "
                    f"exceeds the backbone's {available_full_layers} full-attention layers"
                )
            # HF helpers propagate the function/flag to vision and decoder
            # blocks. The joint decoder bypasses DecoderLayer.forward, so its
            # explicit per-layer schedule below owns decoder recomputation.
            self.qwen.gradient_checkpointing_enable({"use_reentrant": False})
            if not config.gradient_checkpointing_vision:
                self.qwen.visual.gradient_checkpointing_disable()
            if not config.gradient_checkpointing_decoder:
                self.qwen.language_model.gradient_checkpointing_disable()
        if config.compile_model:
            torch.set_float32_matmul_precision("high")
            self._joint_forward = torch.compile(self._joint_forward, dynamic=True, mode=config.compile_mode)

    def train(self, mode: bool = True) -> Qwen3_5ContextVideoKIModel:
        super().train(mode)
        if self.config.freeze_vision_encoder:
            self.qwen.visual.eval()
        if self.config.train_expert_only:
            self.qwen.eval()
        return self

    def _load_from_state_dict(
        self,
        state_dict: dict[str, Tensor],
        prefix: str,
        local_metadata: dict[str, Any],
        strict: bool,
        missing_keys: list[str],
        unexpected_keys: list[str],
        error_msgs: list[str],
    ) -> None:
        version_key = f"{prefix}_unified_token_schema_version"
        checkpoint_version = state_dict.get(version_key)
        if checkpoint_version is not None:
            loaded_version = int(checkpoint_version.item())
            if loaded_version != self.token_schema_version:
                error_msgs.append(
                    f"Unified token schema version mismatch for {version_key}: "
                    f"checkpoint={loaded_version}, expected={self.token_schema_version}"
                )
        nn.Module._load_from_state_dict(
            self, state_dict, prefix, local_metadata, strict, missing_keys, unexpected_keys, error_msgs
        )

    # ---- IBQ bodies (unified_cond) ----------------------------------------------






    # ---- prefix embedding ---------------------------------------------------

    def embed_inputs(
        self,
        *,
        input_ids: Tensor,
        attention_mask: Tensor,
        state: Tensor,
        pixel_values: Tensor,
        image_grid_thw: Tensor,
        pixel_values_videos: Tensor | None = None,
        video_grid_thw: Tensor | None = None,
    ) -> tuple[Tensor, Tensor, Tensor]:
        """Embed all ids from Qwen's tied table, then override marker, vision and state slots.

        Returns ``(inputs_embeds, safe_ids, flat_image_grid)``; ``safe_ids`` has
        the marker ids and padding zeroed, which is what ``get_rope_index`` reads.
        """

        batch_size = input_ids.shape[0]
        expected_state_shape = (batch_size, self.config.max_state_dim)
        if state.ndim != 2 or state.shape != expected_state_shape:
            raise ValueError(f"Expected state {expected_state_shape}, got {tuple(state.shape)}")
        if input_ids.shape != attention_mask.shape:
            raise ValueError("input_ids and attention_mask must share shape [B,L]")

        # Marker ids sit above the table on purpose; sanitize them before the lookup.
        marker_mask = attention_mask & (input_ids >= CTX_TOKEN_START) & (input_ids < CTX_VIDEO_TOKEN_END)
        base_id_mask = (input_ids >= 0) & (input_ids < self.vocab_size)
        invalid = attention_mask & ~base_id_mask & ~marker_mask
        if bool(invalid.any()):
            bad_id = int(input_ids[invalid][0])
            raise ValueError(f"Attended token id {bad_id} is neither a Qwen/FAST/control id nor a ctx marker")
        safe_ids = input_ids.masked_fill(marker_mask | ~attention_mask, 0)
        inputs_embeds = self.qwen.get_input_embeddings()(safe_ids)
        if bool(marker_mask.any()):
            marker_indices = (input_ids[marker_mask] - CTX_TOKEN_START).long()
            marker_embeds = self.context_marker_embeddings(marker_indices)
            inputs_embeds = inputs_embeds.clone()
            inputs_embeds[marker_mask] = marker_embeds.to(device=inputs_embeds.device, dtype=inputs_embeds.dtype)

        flat_grid = image_grid_thw.reshape(-1, 3)
        if pixel_values.ndim > 2:
            pixel_values = pixel_values.reshape(-1, pixel_values.shape[-1])
        image_mask = attention_mask & (safe_ids == int(self.qwen.config.image_token_id))
        image_embeds = self.qwen.get_image_features(
            pixel_values=pixel_values, image_grid_thw=flat_grid, return_dict=True
        ).pooler_output
        if isinstance(image_embeds, (tuple, list)):
            image_embeds = torch.cat(tuple(image_embeds), dim=0)
        if int(image_mask.sum()) != image_embeds.shape[0]:
            raise ValueError(
                "Qwen image placeholder count does not match visual embedding count: "
                f"{int(image_mask.sum())} vs {image_embeds.shape[0]}"
            )
        inputs_embeds = inputs_embeds.masked_scatter(
            image_mask.unsqueeze(-1).expand_as(inputs_embeds),
            image_embeds.to(device=inputs_embeds.device, dtype=inputs_embeds.dtype),
        )

        # Video blocks are optional.  Qwen3.5 renders one video as ceil(T/2)
        # separate vision spans filled with ``video_pad``, so the placeholder
        # count check below is the same total-count invariant used for images.
        video_token_id = getattr(self.qwen.config, "video_token_id", None)
        video_mask = (
            attention_mask & (safe_ids == int(video_token_id))
            if video_token_id is not None
            else torch.zeros_like(attention_mask)
        )
        flat_video_grid = _flat_video_grid(video_grid_thw)
        if flat_video_grid is not None:
            if pixel_values_videos is None:
                raise ValueError("video_grid_thw was provided without pixel_values_videos")
            if pixel_values_videos.ndim > 2:
                pixel_values_videos = pixel_values_videos.reshape(-1, pixel_values_videos.shape[-1])
            video_embeds = self.qwen.get_video_features(
                pixel_values_videos=pixel_values_videos, video_grid_thw=flat_video_grid, return_dict=True
            ).pooler_output
            if isinstance(video_embeds, (tuple, list)):
                video_embeds = torch.cat(tuple(video_embeds), dim=0)
            if int(video_mask.sum()) != video_embeds.shape[0]:
                raise ValueError(
                    "Qwen video placeholder count does not match visual embedding count: "
                    f"{int(video_mask.sum())} vs {video_embeds.shape[0]}"
                )
            inputs_embeds = inputs_embeds.masked_scatter(
                video_mask.unsqueeze(-1).expand_as(inputs_embeds),
                video_embeds.to(device=inputs_embeds.device, dtype=inputs_embeds.dtype),
            )
        elif bool(video_mask.any()):
            raise ValueError("Prompt contains video placeholders but no video grid was supplied")

        # Robot prompts carry exactly one learned-state token; VQA prompts carry none.
        state_mask = attention_mask & (safe_ids == self.special_tokens.learned_state)
        state_rows = state_mask.any(dim=1)
        if not bool((state_mask.sum(dim=1) <= 1).all()):
            raise ValueError("A prompt can contain at most one learned-state token")
        state_embeds = self.state_projection(
            state.to(device=self.state_projection.weight.device, dtype=self.state_projection.weight.dtype)
        )
        inputs_embeds = inputs_embeds.clone()
        inputs_embeds[state_mask] = state_embeds[state_rows].to(device=inputs_embeds.device, dtype=inputs_embeds.dtype)
        return inputs_embeds, safe_ids, flat_grid

    def _build_position_ids(
        self,
        safe_ids: Tensor,
        flat_grid: Tensor,
        attention_mask: Tensor,
        ibq_kind: Tensor | None = None,
        ibq_coords: Tensor | None = None,
        video_grid_thw: Tensor | None = None,
    ) -> tuple[Tensor, Tensor]:
        """The monotonic text plane plus Qwen's three MRoPE planes: ``[4, B, L]``."""
        flat_video_grid = _flat_video_grid(video_grid_thw)
        video_kwargs = {} if flat_video_grid is None else {"video_grid_thw": flat_video_grid}
        vision_position_ids, rope_deltas = self.qwen.model.get_rope_index(
            input_ids=safe_ids, image_grid_thw=flat_grid, attention_mask=attention_mask, **video_kwargs
        )
        text_position_ids = attention_mask.long().cumsum(dim=-1) - 1
        text_position_ids.masked_fill_(~attention_mask, 0)
        vision_position_ids = self._apply_ibq_positions(vision_position_ids, ibq_kind, ibq_coords)
        position_ids = torch.cat((text_position_ids.unsqueeze(0), vision_position_ids), dim=0)
        return position_ids, rope_deltas

    def _apply_ibq_positions(
        self, vision_position_ids: Tensor, ibq_kind: Tensor | None, ibq_coords: Tensor | None
    ) -> Tensor:
        """Lay a ``(view, row, col)`` grid over the IBQ bodies (unified_cond).

        Without this the codec tokens are plain text positions, so the generated
        region has no 2-D structure.  A conditioning view and the future view it
        conditions share one anchor (the plain position of the sample's first
        body token) and therefore one ``(view, row, col)``.
        """
        if ibq_kind is None or not self.config.ibq_grid_positions:
            return vision_position_ids
        body_mask = ibq_kind != IBQ_KIND_NONE
        if not bool(body_mask.any()):
            return vision_position_ids
        first_body = body_mask.int().argmax(dim=1, keepdim=True)
        base = vision_position_ids[0].gather(1, first_body)
        grid = base.unsqueeze(0) + ibq_coords.permute(2, 0, 1).to(vision_position_ids.dtype)
        return torch.where(body_mask.unsqueeze(0), grid, vision_position_ids)

    def embed_prefix(
        self, batch: dict[str, Tensor], *, input_ids: Tensor | None = None
    ) -> tuple[Tensor, Tensor, Tensor, Tensor, Tensor]:
        """Prompt embeddings and positions for a training sequence or an inference prefix.

        ``input_ids`` overrides the batch's ids once the IBQ bodies are materialized.
        Returns ``(inputs_embeds, attention_mask, position_ids, rope_deltas, safe_ids)``.
        """
        if input_ids is None:
            input_ids = batch[f"{OBS_STR}.input_ids"].to(dtype=torch.long)
        attention_mask = batch[f"{OBS_STR}.attention_mask"].to(dtype=torch.bool)
        inputs_embeds, safe_ids, flat_grid = self.embed_inputs(
            input_ids=input_ids,
            attention_mask=attention_mask,
            state=batch[OBS_STATE],
            pixel_values=batch[f"{OBS_STR}.pixel_values"],
            image_grid_thw=batch[f"{OBS_STR}.image_grid_thw"],
            pixel_values_videos=batch.get(PIXEL_VALUES_VIDEOS_KEY),
            video_grid_thw=batch.get(VIDEO_GRID_THW_KEY),
        )
        ibq_kind, ibq_coords = batch.get(IBQ_KIND_KEY), batch.get(IBQ_COORDS_KEY)
        position_ids, rope_deltas = self._build_position_ids(
            safe_ids,
            flat_grid,
            attention_mask,
            ibq_kind=None if ibq_kind is None else ibq_kind.to(device=safe_ids.device, dtype=torch.long),
            ibq_coords=None if ibq_coords is None else ibq_coords.to(device=safe_ids.device, dtype=torch.long),
            video_grid_thw=batch.get(VIDEO_GRID_THW_KEY),
        )
        return inputs_embeds, attention_mask, position_ids, rope_deltas, safe_ids


    # ---- FAST and future-image cross-entropy (unified_cond) ---------------------




    # ---- suffix -------------------------------------------------------------

    def sample_noise(self, shape, device: torch.device) -> Tensor:
        return torch.normal(mean=0.0, std=1.0, size=shape, dtype=torch.float32, device=device)


    def embed_suffix(self, state: Tensor, noisy_actions: Tensor, timestep: Tensor) -> tuple[Tensor, Tensor, Tensor]:
        """``[state] + [chunk_size action-time tokens]``, as vla_ki with tokenize_state=False.

        The state token opens its own attention block; the action tokens form
        one bidirectional block that sees the state and (via the joint mask)
        the prefix.  Returns float32 embeddings, a pad mask and the block mask.
        """
        # Inputs follow the projections' own dtype, so ``policy.to(bfloat16)`` keeps working.
        state_emb = self.state_proj(state.to(self.state_proj.weight.dtype))
        time_emb = create_sinusoidal_pos_embedding(
            timestep,
            self.action_in_proj.out_features,
            min_period=self.config.min_period,
            max_period=self.config.max_period,
            device=timestep.device,
        ).to(self.action_in_proj.weight.dtype)
        action_emb = self.action_in_proj(noisy_actions.to(self.action_in_proj.weight.dtype))
        action_time_emb = torch.cat((action_emb, time_emb[:, None, :].expand_as(action_emb)), dim=2)
        action_time_emb = self.action_time_mlp_out(F.silu(self.action_time_mlp_in(action_time_emb)))

        embs = torch.cat((state_emb[:, None, :], action_time_emb), dim=1)
        bsize, suffix_len = embs.shape[:2]
        pad_masks = torch.ones(bsize, suffix_len, dtype=torch.bool, device=embs.device)
        att_masks = torch.tensor([1, 1] + [0] * (self.config.chunk_size - 1), dtype=torch.long, device=embs.device)
        return embs, pad_masks, att_masks[None, :].expand(bsize, suffix_len)

    # ---- masks and positions ---------------------------------------------------

    def _segment_start_ids(self, like: Tensor) -> Tensor:
        return like.new_tensor(
            [
                self.special_tokens.subtask_start,
                self.special_tokens.cond_start,
                self.special_tokens.future_start,
                self.special_tokens.action_start,
            ]
        )



    @staticmethod
    def _additive_mask(att_2d_masks: Tensor) -> Tensor:
        """``[B, 1, Q, K]`` float mask; finite, so a fully masked padding row stays NaN-free."""
        return torch.where(att_2d_masks[:, None], 0.0, OPENPI_ATTENTION_MASK_VALUE)

    # ---- joint training pass --------------------------------------------------



    _POSITIONAL_KEYS = (
        f"{OBS_STR}.input_ids", f"{OBS_STR}.attention_mask", "labels", "labels.loss_kind", IBQ_KIND_KEY, IBQ_COORDS_KEY,
    )



    # ---- inference prefix -------------------------------------------------------

    def _prepare_generation_prefix(
        self, batch: dict[str, Tensor], *, requested_order: int
    ) -> tuple[dict[str, Tensor], list[list[int]]]:
        """Cut each row to its semantic prefix and left-pad the batch.

        The cond frames and the future-view bookkeeping ride along so the
        future-first image stage can extend the prefix.
        """
        device = self.qwen.get_input_embeddings().weight.device
        input_ids = torch.as_tensor(batch[f"{OBS_STR}.input_ids"], device=device).long()
        attention_mask = torch.as_tensor(batch[f"{OBS_STR}.attention_mask"], device=device).bool()
        if input_ids.ndim != 2 or attention_mask.shape != input_ids.shape:
            raise ValueError("Generation input_ids and attention_mask must share shape [B,L]")
        selected_mode_token = (
            self.special_tokens.image_action if requested_order == ORDER_FUTURE_FIRST else self.special_tokens.action_image
        )
        semantic_ids = [
            self._extract_semantic_prefix(input_ids, attention_mask, batch_index, selected_mode_token=selected_mode_token)
            for batch_index in range(input_ids.shape[0])
        ]
        compact_ids, compact_mask = self._left_pad_prefixes(semantic_ids, device)
        prefix_batch = {
            OBS_STATE: torch.as_tensor(batch[OBS_STATE], device=device),
            f"{OBS_STR}.pixel_values": torch.as_tensor(batch[f"{OBS_STR}.pixel_values"], device=device),
            f"{OBS_STR}.image_grid_thw": torch.as_tensor(batch[f"{OBS_STR}.image_grid_thw"], device=device).long(),
            f"{OBS_STR}.input_ids": compact_ids,
            f"{OBS_STR}.attention_mask": compact_mask,
        }
        optional_keys = [PIXEL_VALUES_VIDEOS_KEY, VIDEO_GRID_THW_KEY, FUTURE_VIEW_INDICES_KEY, FUTURE_GRID_HW_KEY]
        optional_keys.extend((IBQ_FRAMES_KEY, IBQ_FRAME_META_KEY, IBQ_FRAME_COUNT_KEY))
        for key in optional_keys:
            if key in batch:
                prefix_batch[key] = torch.as_tensor(batch[key], device=device)
        return prefix_batch, semantic_ids

    def _extract_semantic_prefix(
        self, input_ids: Tensor, attention_mask: Tensor, batch_index: int, *, selected_mode_token: int
    ) -> list[int]:
        """Everything before the first segment-start token after the predict-mode token."""
        mode_tokens = (self.special_tokens.action_image, self.special_tokens.image_action)
        attended_positions = attention_mask[batch_index].nonzero(as_tuple=False).flatten()
        if attended_positions.numel() == 0:
            raise ValueError("The inference prefix cannot be empty")
        valid_end = int(attended_positions[-1]) + 1
        row_mode_mask = attention_mask[batch_index] & torch.isin(input_ids[batch_index], input_ids.new_tensor(mode_tokens))
        row_mode_positions = row_mode_mask.nonzero(as_tuple=False).flatten()
        if row_mode_positions.numel() != 1:
            raise ValueError("Each inference prefix must contain one predict-mode token")
        mode_position = int(row_mode_positions.item())
        suffix_start = attention_mask[batch_index] & torch.isin(input_ids[batch_index], self._segment_start_ids(input_ids))
        suffix_positions = suffix_start.nonzero(as_tuple=False).flatten()
        suffix_positions = suffix_positions[suffix_positions > mode_position]
        end = int(suffix_positions[0]) if suffix_positions.numel() else valid_end
        if not bool(attention_mask[batch_index, :end].all()):
            raise ValueError("The semantic inference prefix cannot contain masked holes")
        ids = input_ids[batch_index, :end].clone()
        if int((ids == self.special_tokens.learned_state).sum()) != 1:
            raise ValueError("Each inference prefix must contain exactly one learned state token")
        base_ids = (ids >= 0) & (ids < IBQ_TOKEN_START)
        ctx_ids = (ids >= CTX_TOKEN_START) & (ids < CTX_VIDEO_TOKEN_END)
        if not bool((base_ids | ctx_ids).all()):
            raise ValueError("The context prefix contains an IBQ or unknown model-only id")
        if int(ids[mode_position]) != selected_mode_token:
            raise ValueError(
                "inference_order does not match the processed prompt; rebuild it with "
                "Qwen3_5ContextVideoKIProcessorTransformFn.build_inference()"
            )
        return ids.tolist()

    @staticmethod
    def _left_pad_prefixes(semantic_ids: list[list[int]], device: torch.device) -> tuple[Tensor, Tensor]:
        batch_size = len(semantic_ids)
        prefix_length = max(len(ids) for ids in semantic_ids)
        compact_ids = torch.zeros((batch_size, prefix_length), dtype=torch.long, device=device)
        compact_mask = torch.zeros((batch_size, prefix_length), dtype=torch.bool, device=device)
        for batch_index, ids in enumerate(semantic_ids):
            start = prefix_length - len(ids)
            compact_ids[batch_index, start:] = torch.tensor(ids, device=device)
            compact_mask[batch_index, start:] = True
        return compact_ids, compact_mask

    def _prefill_generation(self, batch: dict[str, Tensor]) -> tuple[Tensor, Any, Tensor, Tensor, Tensor]:
        """Run the language model over a prepared prefix with a KV cache.

        Returns ``(last_hidden, past_key_values, attention_mask, position_ids, rope_deltas)``.
        """
        inputs_embeds, attention_mask, position_ids, rope_deltas, _ = self.embed_prefix(batch)
        outputs = self.qwen.model.language_model(
            inputs_embeds=inputs_embeds,
            attention_mask=attention_mask,
            position_ids=position_ids,
            cache_position=torch.arange(inputs_embeds.shape[1], device=inputs_embeds.device, dtype=torch.long),
            use_cache=True,
            return_dict=True,
        )
        if outputs.past_key_values is None:
            raise RuntimeError("Qwen3.5 did not return a KV cache during prefill")
        return outputs.last_hidden_state[:, -1], outputs.past_key_values, attention_mask, position_ids, rope_deltas

    # ---- inference: the prefix stage --------------------------------------------

    @staticmethod
    def _cache_tail_positions(attention_mask: Tensor, rope_deltas: Tensor) -> Tensor:
        """``[4, B, 1]`` position of the last cached token, the anchor the suffix continues from.

        Text plane: attended count - 1.  Vision planes: cache length - 1 plus
        the rope delta, which is what ``get_rope_index`` assigns a plain token
        there; the IBQ grid never touches boundary tokens, so this also holds
        for ``<|unified_future_end|>`` after the image stage.
        """
        text = (attention_mask.long().sum(dim=-1) - 1).view(1, -1, 1)
        vision = (attention_mask.shape[1] - 1 + rope_deltas[:, 0]).view(1, -1, 1).expand(3, -1, -1)
        return torch.cat((text, vision), dim=0)







    def _language_request(self, batch: dict[str, Tensor], bsize: int) -> tuple[list[list[int]], list[bool]]:
        """Per row: the forced segment ids from ``build_inference`` and whether the model decodes the rest.

        A batch without the request (a hand-built prefix) carries no language segment.
        """
        if LANGUAGE_IDS_KEY not in batch:
            return [[] for _ in range(bsize)], [False] * bsize
        ids = torch.as_tensor(batch[LANGUAGE_IDS_KEY]).long().reshape(bsize, -1).tolist()
        lengths = torch.as_tensor(batch[LANGUAGE_LENGTH_KEY]).long().reshape(bsize).tolist()
        generate = torch.as_tensor(batch[LANGUAGE_GENERATE_KEY]).bool().reshape(bsize).tolist()
        return [row[:length] for row, length in zip(ids, lengths, strict=True)], generate



    def _prefix_stage(self, batch, *, requested_order):
        if requested_order != ORDER_ACTION_FIRST:
            raise ValueError("history deployment only supports action_first")
        prepared, semantic_ids = self._prepare_generation_prefix(batch, requested_order=requested_order)
        forced, generate = self._language_request(batch, len(semantic_ids))
        if any(forced) or any(generate):
            raise ValueError("language generation is not enabled in history deployment")
        hidden, past_key_values, attention_mask, _, rope_deltas = self._prefill_generation(prepared)
        anchor = self._cache_tail_positions(attention_mask, rope_deltas)
        return hidden, past_key_values, attention_mask, rope_deltas, anchor, None, None


    # ---- flow-matching inference ------------------------------------------------

    @torch.no_grad()
    def sample_actions(
        self,
        batch: dict[str, Tensor],
        *,
        noise: Tensor | None = None,
        num_steps: int | None = None,
        inference_order: str | None = None,
        return_future_tokens: bool = False,
        return_subtask_ids: bool = False,
    ) -> Tensor | tuple:
        """One VLM prefix stage, then Euler steps of the expert alone against its cache.

        The chunk alone, or a tuple that appends the future tokens and the
        subtask ids in that order for each ``return_*`` flag set.
        """
        with _inference_autocast(self):
            requested_order = order_from_name(inference_order or self.config.inference_order)
            _, past_key_values, attention_mask, _, anchor, future_tokens, subtask_ids = self._prefix_stage(
                batch, requested_order=requested_order
            )
            state = torch.as_tensor(batch[OBS_STATE], device=anchor.device).to(torch.float32)
            bsize, device = state.shape[0], state.device
            if noise is None:
                noise = self.sample_noise((bsize, self.config.chunk_size, self.config.max_action_dim), device)
            if num_steps is None:
                num_steps = self.config.num_inference_steps

            dt = -1.0 / num_steps
            x_t = noise.to(torch.float32)
            time = torch.tensor(1.0, dtype=torch.float32, device=device)
            while time >= -dt / 2:
                v_t = self.denoise_step(state, attention_mask, past_key_values, anchor, x_t, time.expand(bsize))
                x_t = x_t + dt * v_t
                time = time + dt
            return _with_prefix_outputs(x_t, future_tokens, subtask_ids, return_future_tokens, return_subtask_ids)

    def denoise_step(
        self, state: Tensor, prefix_pad_masks: Tensor, past_key_values, anchor: Tensor, x_t: Tensor, timestep: Tensor
    ) -> Tensor:
        suffix_embeds, suffix_pad_masks, suffix_att_masks = self.embed_suffix(state, x_t, timestep)
        suffix_embeds = suffix_embeds.to(dtype=self.qwen.get_input_embeddings().weight.dtype)
        bsize, suffix_len = suffix_pad_masks.shape
        prefix_len = prefix_pad_masks.shape[1]

        # The cache holds only what the expert may see (no FAST segment), so
        # nothing to block: every attended cached token is visible.
        prefix_2d_masks = prefix_pad_masks[:, None, :].expand(bsize, suffix_len, prefix_len)
        suffix_2d_masks = make_att_2d_masks(suffix_pad_masks, suffix_att_masks)
        att_2d_masks = torch.cat((prefix_2d_masks, suffix_2d_masks), dim=2)
        position_ids = anchor + torch.arange(1, suffix_len + 1, device=anchor.device)

        # Reads the VLM cache without updating it (the transformers_replace
        # use_cache=False branch), so every step sees the same prefix.
        outputs = self.action_expert(
            inputs_embeds=suffix_embeds,
            attention_mask=self._additive_mask(att_2d_masks),
            position_ids=position_ids,
            past_key_values=past_key_values,
            use_cache=False,
            return_dict=True,
        )
        suffix_out = outputs.last_hidden_state[:, -self.config.chunk_size :]
        return _fp32_head(self.action_out_proj, suffix_out)

    # ---- greedy FAST decode (the backbone's own head, for A/B) ----------------------





# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


class Qwen3_5ContextVideoKIPolicy(PreTrainedPolicy):  # noqa: N801
    """Trainable/deployable context-video policy with a KI flow-matching action expert."""

    config_class = Qwen3_5ContextVideoKIConfig
    name = "qwen3_5_context_video_ki"

    @classmethod
    def from_pretrained(cls, pretrained_name_or_path, *, strict: bool = True, **kwargs) -> Qwen3_5ContextVideoKIPolicy:
        return super().from_pretrained(pretrained_name_or_path, strict=strict, **kwargs)

    def __init__(
        self,
        config: Qwen3_5ContextVideoKIConfig,
        *,
        model: Qwen3_5ContextVideoKIModel | None = None,
        qwen: nn.Module | None = None,
        tokenizer: Any | None = None,
        special_tokens: UnifiedSpecialTokenIds | None = None,
        image_codec: Any | None = None,
        fast_codec: Any | None = None,
    ) -> None:
        super().__init__(config)
        config.validate_features()
        if model is not None and any(
            dependency is not None for dependency in (qwen, tokenizer, special_tokens, image_codec)
        ):
            raise ValueError("Pass either an injected core model or its dependencies, not both")
        self.model = model or Qwen3_5ContextVideoKIModel(
            config, qwen=qwen, tokenizer=tokenizer, special_tokens=special_tokens, image_codec=image_codec
        )
        self.__dict__["_fast_codec"] = fast_codec
        if config.device is not None:
            self.to(config.device)
        self.reset()

    def __str__(self) -> str:
        total_params = sum(parameter.numel() for parameter in self.parameters())
        trainable_params = sum(parameter.numel() for parameter in self.parameters() if parameter.requires_grad)
        qwen_params = sum(parameter.numel() for parameter in self.model.qwen.parameters())
        expert_params = sum(parameter.numel() for parameter in self.model.action_expert.parameters())
        expert_config = self.model.action_expert.config
        resize = (
            f"pad {self.config.image_resolution}"
            if self.config.image_resize_mode == "pad"
            else f"long_side {self.config.image_resize_long_side}"
        )
        ibq_pad = self.config.ibq_image_resize_mode == "pad"
        ibq_resize = f"pad {self.config.image_resolution}" if ibq_pad else "follows ViT input"
        lines = [
            "=" * 60,
            f"Policy: {self.__class__.__name__}",
            "Parameter statistics:",
            f"  - Policy params       : {format_big_number(total_params)}",
            f"  - Trainable params    : {format_big_number(trainable_params)}",
            f"  - Qwen3.5 params      : {format_big_number(qwen_params)}",
            f"  - Action expert params: {format_big_number(expert_params)}",
            "Backbone:",
            f"  - Model vocab         : {self.model.vocab_size}",
            f"  - VLM                 : {self.config.vlm_model_name_or_path}",
            f"  - FAST tokenizer      : {self.config.action_tokenizer_name}",
            f"  - Resize              : {resize}",
            f"  - IBQ frame resize    : {ibq_resize}",
            f"  - Context slots       : {self.config.context_slot_count} x stride {self.config.context_slot_stride}",
            f"  - FAST CE domain      : {self.config.loss_softmax_domain} (weight {self.config.action_loss_weight})",
            f"  - Future images       : {self.config.predict_future_images} (views {self.config.future_image_indices}, "
            f"weight {self.config.future_loss_weight}, p_future_first {self.config.future_first_probability}, "
            f"inference {self.config.inference_order})",
            "Action expert:",
            f"  - Expert hidden/inter : {expert_config.hidden_size} / {expert_config.intermediate_size}",
            f"  - Knowledge insulation: {self.config.knowledge_insulation}",
            f"  - Block action->fast  : {self.config.block_action_attend_fast_tokens}",
            f"  - Inference action    : {self.config.inference_action_type} ({self.config.num_inference_steps} steps)",
            f"  - FM loss weight      : {self.config.fm_loss_weight}",
            f"  - FM samples / obs    : {self.config.num_flow_matching_samples}",
            f"  - Dtype / attention   : {self.config.dtype} / {self.config.attn_implementation}",
            "=" * 60,
        ]
        return "\n".join(lines)

    def get_optim_params(self, *args, **kwargs):
        raise NotImplementedError("action-only inference deployment")

    # The expert-side small projections are created float32 while the backbone
    # carries config.dtype; every runtime cast already follows each weight's
    # own dtype, so keeping them float32 across moves is the convention this
    # policy documents (vla_ki pins the same path unconditionally).
    _FP32_ACTION_PATH_MODULES = (
        "state_proj",
        "action_in_proj",
        "action_out_proj",
        "action_time_mlp_in",
        "action_time_mlp_out",
    )

    def to(self, *args, **kwargs) -> Qwen3_5ContextVideoKIPolicy:
        # Preserve the action path's intentional FP32 dtype for ordinary
        # device-only moves. DeepSpeed first calls ``module.bfloat16()`` and
        # then ``module.to(device)``; in that sequence the path is already
        # BF16 and must not be silently promoted back to FP32, otherwise a
        # single AdamW group contains mixed dtypes before ZeRO flattening.
        preserve_action_path_fp32 = self.model.action_out_proj.weight.dtype == torch.float32
        super().to(*args, **kwargs)
        if preserve_action_path_fp32:
            for module_name in self._FP32_ACTION_PATH_MODULES:
                getattr(self.model, module_name).to(torch.float32)
        return self

    def reset(self) -> None:
        self._action_queue = deque(maxlen=self.config.n_action_steps)

    def forward(self, *args, **kwargs):
        raise NotImplementedError("action-only inference deployment")

    def _decode_fast_tokens(self, *args, **kwargs):
        raise NotImplementedError("action-only inference deployment")

    @torch.no_grad()
    def predict_action_chunk(self, batch: dict[str, Tensor], **kwargs: Unpack[ActionSelectKwargs]) -> Tensor:
        self.eval()
        if self.config.inference_action_type == "fast":
            actions = self._decode_fast_tokens(self.model.generate_fast_tokens(batch))
        else:
            actions = self.model.sample_actions(batch)
        actions = actions.to(device=batch[OBS_STATE].device, dtype=torch.float32)
        action_dim = self.config.output_features[ACTION].shape[0]
        if action_dim > self.config.max_action_dim:
            raise ValueError(f"Configured action dim {action_dim} exceeds action dim {self.config.max_action_dim}")
        return actions[:, :, :action_dim]

    @torch.no_grad()
    def select_action(self, batch: dict[str, Tensor], **kwargs: Unpack[ActionSelectKwargs]) -> Tensor:
        self.eval()
        if not self._action_queue:
            actions = self.predict_action_chunk(batch, **kwargs)
            self._action_queue.extend(actions[:, : self.config.n_action_steps].transpose(0, 1))
        return self._action_queue.popleft()
