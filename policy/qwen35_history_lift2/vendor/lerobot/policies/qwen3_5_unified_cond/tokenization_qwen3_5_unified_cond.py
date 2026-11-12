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


from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import torch
from huggingface_hub import snapshot_download
from transformers import AutoProcessor

from lerobot.policies.qwen3_5_unified_cond.ibq_codec import IBQ_VOCAB_SIZE
from lerobot.policies.qwen3_5vla_ki.action_tokens import (
    ACTION_TOKEN_MAX,
    ACTION_TOKEN_MIN,
    NUM_ACTION_TOKENS,
    ensure_qwen35_action_tokens,
)
from lerobot.utils.constants import ACTION

IGNORE_INDEX = -100
FUTURE_TOKEN_PLACEHOLDER = IGNORE_INDEX

LOSS_NONE = 0
LOSS_SUBTASK = 1
LOSS_ACTION = 2
LOSS_FUTURE = 3
# Free-text answer of a VQA sample in mixed pretraining (``build_text_answer``);
# shares the subtask markers and text vocabulary, weighted and logged separately.
LOSS_VQA = 4

# Which IBQ body a position belongs to. Conditioning bodies carry LOSS_NONE, so
# loss_kind alone cannot locate them.
IBQ_KIND_NONE = 0
IBQ_KIND_COND = 1
IBQ_KIND_FUTURE = 2
NO_IBQ_COORD = -1

ORDER_ACTION_FIRST = 0
ORDER_FUTURE_FIRST = 1

# Config-facing names for the two generation orders and their internal ids.
INFERENCE_ORDER_ACTION_FIRST = "action_first"
INFERENCE_ORDER_FUTURE_FIRST = "future_first"
INFERENCE_ORDER_BY_NAME = {
    INFERENCE_ORDER_FUTURE_FIRST: ORDER_FUTURE_FIRST,
    INFERENCE_ORDER_ACTION_FIRST: ORDER_ACTION_FIRST,
}

# Fixed model-global vocabulary layout. IBQ ids remain codec-local until they
# cross the processor/model boundary; the Hugging Face tokenizer never owns
# the final 16K rows.
QWEN_TEXT_VOCAB_SIZE = 248_077
FAST_TOKEN_START = ACTION_TOKEN_MIN
FAST_TOKEN_END = ACTION_TOKEN_MAX + 1
CONTROL_TOKEN_START = FAST_TOKEN_END
CONTROL_TOKEN_END = 250_136
IBQ_TOKEN_OFFSET = CONTROL_TOKEN_END
IBQ_TOKEN_START = IBQ_TOKEN_OFFSET
IBQ_TOKEN_END = IBQ_TOKEN_START + IBQ_VOCAB_SIZE
MODEL_VOCAB_SIZE = IBQ_TOKEN_END

FAST_TOKEN_MIN = FAST_TOKEN_START
FAST_TOKEN_MAX = FAST_TOKEN_END - 1
CONTROL_TOKEN_MIN = CONTROL_TOKEN_START
CONTROL_TOKEN_MAX = CONTROL_TOKEN_END - 1
IBQ_TOKEN_MIN = IBQ_TOKEN_START
IBQ_TOKEN_MAX = IBQ_TOKEN_END - 1
IMAGE_TOKEN_VOCAB_SIZE = IBQ_VOCAB_SIZE
DEFAULT_MAX_FUTURE_GRID_HW = (16, 16)
DEFAULT_FAST_TOKENIZER_NAME = "physical-intelligence/fast"

LEARNED_STATE_TOKEN = "<|learned_state|>"
IMAGE_ACTION_TOKEN = "<|image_action|>"
ACTION_IMAGE_TOKEN = "<|action_image|>"

UNIFIED_SPECIAL_TOKENS = (
    LEARNED_STATE_TOKEN,
    IMAGE_ACTION_TOKEN,
    ACTION_IMAGE_TOKEN,
    "<|unified_subtask_start|>",
    "<|unified_subtask_end|>",
    "<|unified_future_start|>",
    "<|unified_future_end|>",
    "<|unified_action_start|>",
    "<|unified_action_end|>",
    # Appended last so the nine original control ids keep their values.
    "<|unified_cond_start|>",
    "<|unified_cond_end|>",
)


@dataclass(frozen=True)
class UnifiedSpecialTokenIds:
    learned_state: int
    image_action: int
    action_image: int
    subtask_start: int
    subtask_end: int
    future_start: int
    future_end: int
    action_start: int
    action_end: int
    cond_start: int
    cond_end: int


def ensure_unified_special_tokens(tokenizer: Any) -> UnifiedSpecialTokenIds:
    """Install FAST first, then strictly verify the eleven fixed control ids.

    FAST ids are fixed by the existing Qwen3.5 policies. Adding any other token
    before them would shift those ids and silently invalidate old checkpoints.
    IBQ ids are model-only and are deliberately not registered here.
    """

    ensure_qwen35_action_tokens(tokenizer)
    vocabulary = tokenizer.get_vocab()
    present = [token in vocabulary for token in UNIFIED_SPECIAL_TOKENS]
    if any(present) and not all(present):
        raise ValueError("Tokenizer has only a partial qwen3_5_unified_cond control-token set")
    if not any(present):
        tokenizer.add_special_tokens({"additional_special_tokens": list(UNIFIED_SPECIAL_TOKENS)})

    token_ids = [tokenizer.convert_tokens_to_ids(token) for token in UNIFIED_SPECIAL_TOKENS]
    expected_ids = list(range(CONTROL_TOKEN_START, CONTROL_TOKEN_END))
    if token_ids != expected_ids:
        raise ValueError(f"Unified control token ids mismatch: expected {expected_ids}, got {token_ids}")
    if getattr(tokenizer, "unk_token_id", None) in token_ids:
        raise ValueError("At least one unified control token resolved to unk_token_id")
    vocabulary_ids = list(tokenizer.get_vocab().values())
    if any(not isinstance(token_id, int) for token_id in vocabulary_ids):
        raise TypeError("Tokenizer vocabulary ids must be integers")
    if vocabulary_ids and max(vocabulary_ids) >= IBQ_TOKEN_START:
        raise ValueError("The Hugging Face tokenizer must not register model-only IBQ ids")
    return UnifiedSpecialTokenIds(*token_ids)


def _as_integer_tensor(values: Any, *, name: str) -> torch.Tensor:
    """Convert integer-like values without silently truncating floats or bools."""

    if isinstance(values, torch.Tensor):
        tensor = values
    else:
        try:
            tensor = torch.as_tensor(values)
        except (TypeError, ValueError) as error:
            raise TypeError(f"{name} must be convertible to an integer tensor") from error
    if tensor.dtype == torch.bool or tensor.dtype.is_floating_point or tensor.dtype.is_complex:
        raise TypeError(f"{name} must use an integer dtype, got {tensor.dtype}")
    return tensor.to(dtype=torch.long)


def ibq_code_ids_to_global_ids(ibq_code_ids: Any) -> torch.Tensor:
    """Map codec-local IBQ ids in ``[0, 16384)`` into the model vocabulary."""

    code_ids = _as_integer_tensor(ibq_code_ids, name="ibq_code_ids")
    if code_ids.numel() and bool(((code_ids < 0) | (code_ids >= IBQ_VOCAB_SIZE)).any()):
        raise ValueError(f"ibq_code_ids must be in [0, {IBQ_VOCAB_SIZE})")
    return code_ids + IBQ_TOKEN_OFFSET


def global_ids_to_ibq_code_ids(global_ids: Any) -> torch.Tensor:
    """Map model-global IBQ ids back to codec-local ids, with strict checks."""

    token_ids = _as_integer_tensor(global_ids, name="global_ids")
    if token_ids.numel() and bool(((token_ids < IBQ_TOKEN_START) | (token_ids >= IBQ_TOKEN_END)).any()):
        raise ValueError(f"global IBQ ids must be in [{IBQ_TOKEN_START}, {IBQ_TOKEN_END})")
    return token_ids - IBQ_TOKEN_OFFSET


def fast_ids_to_qwen_ids(fast_ids: torch.Tensor) -> torch.Tensor:
    """Shift FAST ids into the Qwen action-token window; raises on out-of-range ids."""
    fast_ids = torch.as_tensor(fast_ids, dtype=torch.long).flatten()
    if fast_ids.numel() == 0:
        raise ValueError("FAST produced an empty token sequence")
    if int(fast_ids.min()) < 0 or int(fast_ids.max()) >= NUM_ACTION_TOKENS:
        raise ValueError(
            f"FAST token ids must be in [0, {NUM_ACTION_TOKENS}), got "
            f"[{int(fast_ids.min())}, {int(fast_ids.max())}]"
        )
    return fast_ids + ACTION_TOKEN_MIN


def qwen_ids_to_fast_ids(qwen_ids: torch.Tensor) -> torch.Tensor:
    """Inverse of fast_ids_to_qwen_ids; raises on ids outside the action-token window."""
    qwen_ids = torch.as_tensor(qwen_ids, dtype=torch.long).flatten()
    if qwen_ids.numel() == 0:
        raise ValueError("Cannot decode an empty Qwen action-token sequence")
    if int(qwen_ids.min()) < ACTION_TOKEN_MIN or int(qwen_ids.max()) > ACTION_TOKEN_MAX:
        raise ValueError(
            f"Qwen action ids must be in [{ACTION_TOKEN_MIN}, {ACTION_TOKEN_MAX}], got "
            f"[{int(qwen_ids.min())}, {int(qwen_ids.max())}]"
        )
    return qwen_ids - ACTION_TOKEN_MIN


def resolve_local_snapshot(model_name_or_path: str) -> Path:
    """Resolve a local directory or an already-downloaded HF snapshot; never downloads."""
    local_path = Path(model_name_or_path).expanduser()
    if local_path.is_dir():
        return local_path.resolve()
    if local_path.is_absolute():
        raise FileNotFoundError(f"Local model directory does not exist: {local_path}")
    return Path(snapshot_download(repo_id=model_name_or_path, local_files_only=True))


# Backward-compatible alias for existing importers (tests).
_resolve_local_snapshot = resolve_local_snapshot




def order_from_name(inference_order: str) -> int:
    """Map a config-facing order name to its internal ORDER_* id."""
    if inference_order not in INFERENCE_ORDER_BY_NAME:
        raise ValueError("inference_order must be either 'future_first' or 'action_first'")
    return INFERENCE_ORDER_BY_NAME[inference_order]


LOSS_SOFTMAX_SEGMENT = "segment"
LOSS_SOFTMAX_UNIFIED = "unified"
LOSS_SOFTMAX_DOMAINS = (LOSS_SOFTMAX_SEGMENT, LOSS_SOFTMAX_UNIFIED)

FUTURE_VIEW_SAMPLING_ALL = "all"
FUTURE_VIEW_SAMPLING_HEAD_ONLY = "head_only"
FUTURE_VIEW_SAMPLING_HEAD_PLUS = "head_plus"
FUTURE_VIEW_SAMPLING_MODES = (
    FUTURE_VIEW_SAMPLING_ALL,
    FUTURE_VIEW_SAMPLING_HEAD_ONLY,
    FUTURE_VIEW_SAMPLING_HEAD_PLUS,
)


def max_predicted_views(candidate_indices: Sequence[int], *, mode: str) -> int:
    """View slots one sample can occupy; sizes the padded per-view tensors."""

    if mode == FUTURE_VIEW_SAMPLING_ALL:
        return len(candidate_indices)
    if mode == FUTURE_VIEW_SAMPLING_HEAD_ONLY:
        return 1
    return min(2, len(candidate_indices))


def sample_future_views(
    candidate_indices: Sequence[int], *, mode: str, draw: int | None = None
) -> tuple[int, ...]:
    """Pick the views one sample predicts: the head camera plus at most one other.

    The head camera is static, so its conditioning/future difference is scene
    dynamics and the copy structure stays clean; wrist cameras move with the arm,
    where ego-motion would dominate the loss. Predicting the head every time keeps
    that signal stable while the random draw still covers every other camera.
    """

    if mode not in FUTURE_VIEW_SAMPLING_MODES:
        raise ValueError(f"future_view_sampling must be one of {FUTURE_VIEW_SAMPLING_MODES}")
    if not candidate_indices:
        raise ValueError("future view sampling needs at least one candidate camera")
    if mode == FUTURE_VIEW_SAMPLING_ALL:
        return tuple(candidate_indices)
    # obs[0] is always the head camera, so the lowest index is the head.
    candidates = sorted(candidate_indices)
    head, others = candidates[:1], candidates[1:]
    if mode == FUTURE_VIEW_SAMPLING_HEAD_ONLY or not others:
        return tuple(head)
    if draw is None:
        draw = int(torch.randint(len(others), ()))
    return (*head, others[draw])


def sample_order(future_first_probability: float, *, draw: float | None = None) -> int:
    """Bernoulli draw of the training segment order; p==0/p==1 skip the RNG entirely."""
    if not 0.0 <= future_first_probability <= 1.0:
        raise ValueError(f"future_first_probability must be in [0,1], got {future_first_probability}")
    if future_first_probability == 0.0:
        return ORDER_ACTION_FIRST
    if future_first_probability == 1.0:
        return ORDER_FUTURE_FIRST
    if draw is None:
        draw = float(torch.rand(()).item())
    return ORDER_FUTURE_FIRST if draw < future_first_probability else ORDER_ACTION_FIRST


def _normalize_grid_hw(
    grid_hw: Any, *, max_num_slots: int, max_grid_hw: tuple[int, int], name: str = "future_grid_hw"
) -> torch.Tensor:
    """Validate a ``[V,2]`` IBQ token grid, with ``V`` a runtime view count."""

    grid_hw = _as_integer_tensor(grid_hw, name=name)
    if grid_hw.ndim != 2 or grid_hw.shape[1] != 2 or not 1 <= grid_hw.shape[0] <= max_num_slots:
        raise ValueError(f"Expected {name} [V,2] with 1 <= V <= {max_num_slots}, got {tuple(grid_hw.shape)}")
    max_hw = grid_hw.new_tensor(max_grid_hw)
    if bool((grid_hw <= 0).any()) or bool((grid_hw > max_hw).any()):
        raise ValueError(f"{name} must be positive and at most {max_grid_hw}")
    return grid_hw


def _normalize_view_indices(view_indices: Any, *, num_views: int, max_num_views: int) -> torch.Tensor:
    """Validate the camera index of every predicted view slot."""

    views = _as_integer_tensor(view_indices, name="view_indices").flatten()
    if views.numel() != num_views or bool(((views < 0) | (views >= max_num_views)).any()):
        raise ValueError(f"Expected {num_views} view indices in [0, {max_num_views}), got {views.tolist()}")
    return views


def _pad_slots(values: torch.Tensor, *, max_num_slots: int, fill: int) -> torch.Tensor:
    """Right-pad a per-view-slot tensor so a variable view count still collates."""

    padding = values.new_full((max_num_slots - values.shape[0], *values.shape[1:]), fill)
    return torch.cat((values, padding), dim=0)


def _prepare_ibq_token_views(
    token_ids: Sequence[torch.Tensor | None], *, grid_hw: torch.Tensor, name: str = "future_token_ids"
) -> list[list[int]]:
    """Validate already-global IBQ token ids and return view-major bodies."""

    if len(token_ids) != grid_hw.shape[0]:
        raise ValueError(f"Expected {grid_hw.shape[0]} {name} views, got {len(token_ids)}")

    bodies: list[list[int]] = []
    for view_tokens, (grid_h, grid_w) in zip(token_ids, grid_hw.tolist(), strict=True):
        num_tokens = grid_h * grid_w
        if view_tokens is None:
            bodies.append([FUTURE_TOKEN_PLACEHOLDER] * num_tokens)
            continue
        tokens = _as_integer_tensor(view_tokens, name=name).flatten()
        if tokens.numel() != num_tokens:
            raise ValueError(f"Expected {num_tokens} image tokens, got {tokens.numel()}")
        # This is the policy-facing boundary: callers must not pass raw codec ids.
        global_ids_to_ibq_code_ids(tokens)
        bodies.append(tokens.tolist())
    return bodies


def _as_text_ids(values: Any, *, name: str) -> torch.Tensor:
    """Flatten token ids that must stay inside the Qwen text vocabulary (subtask or VQA answer)."""

    ids = _as_integer_tensor(values, name=name).flatten()
    if ids.numel() and bool(((ids < 0) | (ids >= QWEN_TEXT_VOCAB_SIZE)).any()):
        raise ValueError(f"{name} must remain inside the Qwen text vocabulary")
    return ids


def _raster_coords(camera_index: int, grid_h: int, grid_w: int) -> list[tuple[int, int, int]]:
    """``(view, row, col)`` for one view body, in the raster order the body uses."""

    return [(camera_index, row, col) for row in range(grid_h) for col in range(grid_w)]


def future_body_mask(loss_kind: torch.Tensor) -> torch.Tensor:
    """Locate teacher-forced future body inputs by shifting ``LOSS_FUTURE`` right."""

    loss_kind = torch.as_tensor(loss_kind)
    if loss_kind.ndim == 0:
        raise ValueError("loss_kind must have a sequence dimension")
    loss_mask = loss_kind == LOSS_FUTURE
    body_mask = torch.zeros_like(loss_mask)
    body_mask[..., 1:] = loss_mask[..., :-1]
    return body_mask


_NO_COORD = (NO_IBQ_COORD, NO_IBQ_COORD, NO_IBQ_COORD)


class _SequenceAccumulator:
    """Parallel input/label/loss-kind/IBQ-layout lists for one causal sequence."""

    def __init__(self) -> None:
        self.input_ids: list[int] = []
        self.labels: list[int] = []
        self.loss_kinds: list[int] = []
        self.ibq_kinds: list[int] = []
        self.ibq_coords: list[tuple[int, int, int]] = []

    def append(
        self,
        input_id: int,
        *,
        label: int = IGNORE_INDEX,
        loss_kind: int = LOSS_NONE,
        ibq_kind: int = IBQ_KIND_NONE,
        ibq_coord: tuple[int, int, int] = _NO_COORD,
    ) -> None:
        self.input_ids.append(int(input_id))
        self.labels.append(int(label))
        self.loss_kinds.append(loss_kind)
        self.ibq_kinds.append(ibq_kind)
        self.ibq_coords.append(ibq_coord)

    def append_teacher_forced_segment(
        self, body: list[int], *, start_token: int, end_token: int, loss_kind: int
    ) -> None:
        inputs = [start_token, *body, end_token]
        labels = [*body, end_token, IGNORE_INDEX]
        for input_id, label in zip(inputs, labels, strict=True):
            supervised = label != IGNORE_INDEX
            self.append(label=label, input_id=input_id, loss_kind=loss_kind if supervised else LOSS_NONE)

    def pad_to(self, length: int) -> int:
        valid_length = len(self.input_ids)
        if valid_length > length:
            raise ValueError(f"Sequence has {valid_length} tokens, exceeding fixed length {length}")
        pad_length = length - valid_length
        self.input_ids.extend([0] * pad_length)
        self.labels.extend([IGNORE_INDEX] * pad_length)
        self.loss_kinds.extend([LOSS_NONE] * pad_length)
        self.ibq_kinds.extend([IBQ_KIND_NONE] * pad_length)
        self.ibq_coords.extend([_NO_COORD] * pad_length)
        return valid_length


@dataclass(frozen=True)
class UnifiedSequenceBuilder:
    special_tokens: UnifiedSpecialTokenIds
    max_prompt_tokens: int = 350
    max_subtask_tokens: int = 64
    max_action_tokens: int = 256
    future_image_indices: tuple[int, ...] = (0,)
    max_future_grid_hw: tuple[int, int] = DEFAULT_MAX_FUTURE_GRID_HW
    max_num_views: int = 3
    max_predicted_views: int = 1
    cond_frame_enabled: bool = True
    # Action-only ablation: no cond/future blocks.
    predict_future_images: bool = True
    # KI-style state text replaces the <|learned_state|> token entirely.
    tokenize_state: bool = False

    def __post_init__(self) -> None:
        control_ids = [
            getattr(self.special_tokens, name) for name in self.special_tokens.__dataclass_fields__
        ]
        if control_ids != list(range(CONTROL_TOKEN_START, CONTROL_TOKEN_END)):
            raise ValueError("Unified special token ids must occupy the fixed control-token interval")
        if not self.future_image_indices or len(set(self.future_image_indices)) != len(
            self.future_image_indices
        ):
            raise ValueError("future_image_indices must be a non-empty sequence of unique indices")
        if any(index < 0 or index >= self.max_num_views for index in self.future_image_indices):
            raise ValueError(f"future_image_indices must be in [0, {self.max_num_views})")
        if not 1 <= self.max_predicted_views <= self.max_num_views:
            raise ValueError(f"max_predicted_views must be in [1, {self.max_num_views}]")
        if len(self.max_future_grid_hw) != 2 or any(size <= 0 for size in self.max_future_grid_hw):
            raise ValueError("max_future_grid_hw must contain two positive values")

    @property
    def max_view_body_tokens(self) -> int:
        """Tokens one padded IBQ block costs, including its two control tokens."""
        return self.max_future_grid_hw[0] * self.max_future_grid_hw[1] + 2

    @property
    def max_sequence_length(self) -> int:
        """Fixed training length. The per-segment budgets only size this pool: a
        subtask or FAST stream may exceed its own budget as long as the whole
        sequence fits (``_SequenceAccumulator.pad_to`` raises otherwise). The
        prompt keeps its own cap because inference pads the prefix to it."""
        blocks_per_view = 2 if self.cond_frame_enabled else 1
        image_tokens = (
            blocks_per_view * self.max_predicted_views * self.max_view_body_tokens
            if self.predict_future_images
            else 0
        )
        return (
            self.max_prompt_tokens
            + (self.max_subtask_tokens + 2)
            + image_tokens
            + (self.max_action_tokens + 2)
        )

    @property
    def max_inference_prefix_length(self) -> int:
        return self.max_prompt_tokens

    def _mode_token(self, requested_order: int) -> int:
        if requested_order == ORDER_FUTURE_FIRST:
            return self.special_tokens.image_action
        if requested_order == ORDER_ACTION_FIRST:
            return self.special_tokens.action_image
        raise ValueError(f"Unknown order id: {requested_order}")

    def _validate_prefix_state_count(self, state_count: int) -> None:
        """Subclasses override to allow a different <|learned_state|> multiplicity."""
        expected_state_tokens = 0 if self.tokenize_state else 1
        if state_count != expected_state_tokens:
            raise ValueError(
                f"Prompt must contain exactly {expected_state_tokens} {LEARNED_STATE_TOKEN} token(s)"
            )

    def _checked_prompt(self, prefix_input_ids: torch.Tensor, *, state_tokens: int | None = None) -> torch.Tensor:
        """Flatten the prompt ids and check the prompt budget and the state-token count.

        ``state_tokens`` overrides the policy's own expectation (a VQA prompt carries none).
        """
        prefix_input_ids = _as_integer_tensor(prefix_input_ids, name="prefix_input_ids").flatten()
        if prefix_input_ids.numel() > self.max_prompt_tokens:
            raise ValueError(
                f"Prompt has {prefix_input_ids.numel()} tokens, exceeding "
                f"max_prompt_tokens={self.max_prompt_tokens}"
            )
        state_count = int((prefix_input_ids == self.special_tokens.learned_state).sum())
        if state_tokens is None:
            self._validate_prefix_state_count(state_count)
        elif state_count != state_tokens:
            raise ValueError(f"Prompt must contain exactly {state_tokens} {LEARNED_STATE_TOKEN} token(s)")
        return prefix_input_ids

    def _prepare_prefix(self, prefix_input_ids: torch.Tensor, requested_order: int) -> torch.Tensor:
        prefix_input_ids = self._checked_prompt(prefix_input_ids)
        mode_mask = (prefix_input_ids == self.special_tokens.image_action) | (
            prefix_input_ids == self.special_tokens.action_image
        )
        mode_positions = mode_mask.nonzero(as_tuple=False)
        if mode_positions.numel() != 1:
            raise ValueError("Prompt must contain exactly one predict-mode token")
        if int(prefix_input_ids[int(mode_positions.item())]) != self._mode_token(requested_order):
            raise ValueError("Prompt predict mode does not match requested_order")
        return prefix_input_ids

    def _normalize_views(self, future_grid_hw: Any, view_indices: Any) -> tuple[torch.Tensor, torch.Tensor]:
        """Validate the runtime view set; view count is never assumed constant."""

        if view_indices is None:
            # ``future_image_indices`` is the candidate set once views are sampled.
            view_indices = torch.tensor(self.future_image_indices[: self.max_predicted_views])
        grid_hw = _normalize_grid_hw(
            future_grid_hw, max_num_slots=self.max_predicted_views, max_grid_hw=self.max_future_grid_hw
        )
        views = _normalize_view_indices(
            view_indices, num_views=grid_hw.shape[0], max_num_views=self.max_num_views
        )
        return grid_hw, views

    def build_inference_prefix(
        self,
        *,
        prefix_input_ids: torch.Tensor,
        requested_order: int,
        future_grid_hw: torch.Tensor,
        view_indices: Any = None,
    ) -> dict[str, torch.Tensor]:
        prefix_input_ids = self._prepare_prefix(prefix_input_ids, requested_order)
        future_grid_hw, view_indices = self._normalize_views(future_grid_hw, view_indices)
        input_ids = torch.zeros(self.max_inference_prefix_length, dtype=torch.long)
        valid_length = prefix_input_ids.numel()
        input_ids[:valid_length] = prefix_input_ids
        attention_mask = torch.zeros(self.max_inference_prefix_length, dtype=torch.bool)
        attention_mask[:valid_length] = True
        return {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "ibq_kind": torch.zeros(self.max_inference_prefix_length, dtype=torch.int8),
            "ibq_coords": torch.full((self.max_inference_prefix_length, 3), NO_IBQ_COORD, dtype=torch.int32),
            **self._view_slot_fields(future_grid_hw, view_indices, cond_cached=False),
        }

    def build(
        self,
        *,
        prefix_input_ids: torch.Tensor,
        fast_ids: torch.Tensor,
        subtask_input_ids: torch.Tensor | None,
        requested_order: int,
        future_token_ids: Sequence[torch.Tensor | None],
        future_grid_hw: torch.Tensor,
        cond_token_ids: Sequence[torch.Tensor | None] | None = None,
        view_indices: Any = None,
    ) -> dict[str, torch.Tensor]:
        """Build one fixed-length sequence using model-global token ids."""

        prefix_input_ids = self._prepare_prefix(prefix_input_ids, requested_order)
        future_grid_hw, view_indices = self._normalize_views(future_grid_hw, view_indices)
        qwen_action_ids, subtask_input_ids, future_bodies = self._validate_build_targets(
            fast_ids=fast_ids,
            subtask_input_ids=subtask_input_ids,
            future_token_ids=future_token_ids,
            future_grid_hw=future_grid_hw,
        )
        cond_bodies, cond_cached = self._prepare_cond_context(
            cond_token_ids=cond_token_ids, future_grid_hw=future_grid_hw
        )

        sequence = _SequenceAccumulator()
        for token_id in prefix_input_ids.tolist():
            sequence.append(token_id)

        if subtask_input_ids.numel() > 0:
            sequence.append_teacher_forced_segment(
                subtask_input_ids.tolist(),
                start_token=self.special_tokens.subtask_start,
                end_token=self.special_tokens.subtask_end,
                loss_kind=LOSS_SUBTASK,
            )

        # Tail-anchored chunks and future frames stay supervised: delta sampling
        # clamps the out-of-episode targets to the last frame, so the labels teach
        # "settle at the final pose" exactly like qwen3_5_fast's tail padding does.
        if not self.predict_future_images:
            self._append_action_segment(sequence, qwen_action_ids)
        else:
            views = view_indices.tolist()
            grids = future_grid_hw.tolist()

            def append_image_segments() -> None:
                # Every conditioning block precedes every future block, so each
                # generated view can attend to all conditioning views.
                if self.cond_frame_enabled:
                    self._append_cond_segments(sequence, cond_bodies, views, grids)
                self._append_future_segments(sequence, future_bodies, views, grids)

            if requested_order == ORDER_FUTURE_FIRST:
                append_image_segments()
                self._append_action_segment(sequence, qwen_action_ids)
            else:
                self._append_action_segment(sequence, qwen_action_ids)
                append_image_segments()
        return self._materialize(sequence, future_grid_hw, view_indices, cond_cached=cond_cached)

    def build_text_answer(
        self, *, prefix_input_ids: torch.Tensor, answer_input_ids: torch.Tensor
    ) -> dict[str, torch.Tensor]:
        """One fixed-length VQA sequence: the prompt, then the answer as a teacher-forced text segment.

        The answer reuses the subtask markers and text vocabulary but is tagged
        ``LOSS_VQA`` so the model weights and logs it on its own.  The prompt
        carries no state slot and no predict-mode token, and the sequence no
        FAST, cond or future block; the view slots hold a dummy grid so the sample
        still collates with robot samples (nothing reads it: no future labels, no
        IBQ bodies).
        """
        prefix_input_ids = self._checked_prompt(prefix_input_ids, state_tokens=0)
        answer_input_ids = _as_text_ids(answer_input_ids, name="answer_input_ids")
        if answer_input_ids.numel() == 0:
            raise ValueError("A VQA answer must contain at least one token")

        sequence = _SequenceAccumulator()
        for token_id in prefix_input_ids.tolist():
            sequence.append(token_id)
        sequence.append_teacher_forced_segment(
            answer_input_ids.tolist(),
            start_token=self.special_tokens.subtask_start,
            end_token=self.special_tokens.subtask_end,
            loss_kind=LOSS_VQA,
        )
        future_grid_hw, view_indices = self._normalize_views(
            torch.ones((1, 2), dtype=torch.long), torch.tensor([self.future_image_indices[0]])
        )
        return self._materialize(sequence, future_grid_hw, view_indices, cond_cached=False)

    def _prepare_cond_context(
        self, *, cond_token_ids: Sequence[torch.Tensor | None] | None, future_grid_hw: torch.Tensor
    ) -> tuple[list[list[int]], bool]:
        """Conditioning views mirror the predicted views one-to-one."""

        if not self.cond_frame_enabled:
            return [], False
        if cond_token_ids is None:
            cond_token_ids = (None,) * future_grid_hw.shape[0]
        cached = all(tokens is not None for tokens in cond_token_ids)
        bodies = _prepare_ibq_token_views(cond_token_ids, grid_hw=future_grid_hw, name="cond_token_ids")
        return bodies, cached

    def _validate_build_targets(
        self,
        *,
        fast_ids: torch.Tensor,
        subtask_input_ids: torch.Tensor | None,
        future_token_ids: Sequence[torch.Tensor | None],
        future_grid_hw: torch.Tensor,
    ) -> tuple[torch.Tensor, torch.Tensor, list[list[int]]]:
        cached_views = [tokens is not None for tokens in future_token_ids]
        if any(cached_views) and not all(cached_views):
            raise ValueError("Future token views must be either all cached or all unresolved")
        future_bodies = _prepare_ibq_token_views(future_token_ids, grid_hw=future_grid_hw)

        qwen_action_ids = fast_ids_to_qwen_ids(fast_ids)
        if subtask_input_ids is None:
            subtask_input_ids = torch.empty(0, dtype=torch.long)
        else:
            subtask_input_ids = _as_text_ids(subtask_input_ids, name="subtask_input_ids")
        return qwen_action_ids, subtask_input_ids, future_bodies

    def _append_cond_segments(
        self,
        sequence: _SequenceAccumulator,
        cond_bodies: list[list[int]],
        views: list[int],
        grids: list[list[int]],
    ) -> None:
        """Append context-only conditioning blocks: no labels, no loss, ever."""

        for body, camera_index, (grid_h, grid_w) in zip(cond_bodies, views, grids, strict=True):
            coords = _raster_coords(camera_index, grid_h, grid_w)
            sequence.append(self.special_tokens.cond_start)
            for input_id, coord in zip(body, coords, strict=True):
                token = IBQ_TOKEN_START if input_id == FUTURE_TOKEN_PLACEHOLDER else input_id
                sequence.append(token, ibq_kind=IBQ_KIND_COND, ibq_coord=coord)
            sequence.append(self.special_tokens.cond_end)

    def _append_future_segments(
        self,
        sequence: _SequenceAccumulator,
        future_bodies: list[list[int]],
        views: list[int],
        grids: list[list[int]],
    ) -> None:
        for body, camera_index, (grid_h, grid_w) in zip(future_bodies, views, grids, strict=True):
            coords = _raster_coords(camera_index, grid_h, grid_w)
            sequence.append(self.special_tokens.future_start, label=body[0], loss_kind=LOSS_FUTURE)
            for offset, (input_id, label) in enumerate(zip(body[:-1], body[1:], strict=True)):
                teacher_input = IBQ_TOKEN_START if input_id == FUTURE_TOKEN_PLACEHOLDER else input_id
                sequence.append(
                    teacher_input,
                    label=label,
                    loss_kind=LOSS_FUTURE,
                    ibq_kind=IBQ_KIND_FUTURE,
                    ibq_coord=coords[offset],
                )
            final_input = IBQ_TOKEN_START if body[-1] == FUTURE_TOKEN_PLACEHOLDER else body[-1]
            sequence.append(final_input, ibq_kind=IBQ_KIND_FUTURE, ibq_coord=coords[-1])
            sequence.append(self.special_tokens.future_end)

    def _append_action_segment(self, sequence: _SequenceAccumulator, qwen_action_ids: torch.Tensor) -> None:
        sequence.append_teacher_forced_segment(
            qwen_action_ids.tolist(),
            start_token=self.special_tokens.action_start,
            end_token=self.special_tokens.action_end,
            loss_kind=LOSS_ACTION,
        )

    def _view_slot_fields(
        self, future_grid_hw: torch.Tensor, view_indices: torch.Tensor, *, cond_cached: bool
    ) -> dict[str, torch.Tensor]:
        """Pad the per-view slots so a variable view count still collates."""

        return {
            "future_grid_hw": _pad_slots(future_grid_hw, max_num_slots=self.max_predicted_views, fill=0),
            "future_view_indices": _pad_slots(
                view_indices, max_num_slots=self.max_predicted_views, fill=NO_IBQ_COORD
            ),
            "cond_cached": torch.tensor(cond_cached),
        }

    def _materialize(
        self,
        sequence: _SequenceAccumulator,
        future_grid_hw: torch.Tensor,
        view_indices: torch.Tensor,
        *,
        cond_cached: bool,
    ) -> dict[str, torch.Tensor]:
        valid_length = sequence.pad_to(self.max_sequence_length)
        attention_mask = torch.zeros(self.max_sequence_length, dtype=torch.bool)
        attention_mask[:valid_length] = True
        return {
            "input_ids": torch.tensor(sequence.input_ids, dtype=torch.long),
            "attention_mask": attention_mask,
            "labels": torch.tensor(sequence.labels, dtype=torch.long),
            "loss_kind": torch.tensor(sequence.loss_kinds, dtype=torch.int8),
            "ibq_kind": torch.tensor(sequence.ibq_kinds, dtype=torch.int8),
            "ibq_coords": torch.tensor(sequence.ibq_coords, dtype=torch.int32),
            **self._view_slot_fields(future_grid_hw, view_indices, cond_cached=cond_cached),
        }


def valid_view_slots(future_view_indices: torch.Tensor) -> torch.Tensor:
    """Boolean mask of the view slots a sample actually uses."""
    return _as_integer_tensor(future_view_indices, name="future_view_indices") >= 0


def _materialize_ibq_bodies(
    sequence: dict[str, torch.Tensor], ibq_code_ids: Sequence[torch.Tensor], *, kind: int
) -> dict[str, torch.Tensor]:
    global_views = tuple(ibq_code_ids_to_global_ids(tokens) for tokens in ibq_code_ids)
    valid = valid_view_slots(sequence["future_view_indices"])
    bodies = _prepare_ibq_token_views(global_views, grid_hw=sequence["future_grid_hw"][valid])
    flattened = torch.tensor([token for body in bodies for token in body], dtype=torch.long)
    output = {
        key: value.clone() if isinstance(value, torch.Tensor) else value for key, value in sequence.items()
    }
    input_mask = output["ibq_kind"] == kind
    if int(input_mask.sum()) != flattened.numel():
        raise ValueError("IBQ sequence layout does not match the supplied image-token views")
    output["input_ids"][input_mask] = flattened
    if kind == IBQ_KIND_FUTURE:
        output["labels"][output["loss_kind"] == LOSS_FUTURE] = flattened
    else:
        output["cond_cached"] = torch.tensor(True)
    return output


def materialize_future_tokens(
    sequence: dict[str, torch.Tensor], future_ibq_code_ids: Sequence[torch.Tensor]
) -> dict[str, torch.Tensor]:
    """Fill unresolved future positions from view-major codec-local IBQ ids."""
    return _materialize_ibq_bodies(sequence, future_ibq_code_ids, kind=IBQ_KIND_FUTURE)


def materialize_cond_tokens(
    sequence: dict[str, torch.Tensor], cond_ibq_code_ids: Sequence[torch.Tensor]
) -> dict[str, torch.Tensor]:
    """Fill unresolved conditioning positions; conditioning never gets labels."""
    return _materialize_ibq_bodies(sequence, cond_ibq_code_ids, kind=IBQ_KIND_COND)
