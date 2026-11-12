#!/usr/bin/env python

"""Configuration for the video-packed context policy.

v2 inherits straight from ``qwen3_5_unified_cond`` instead of from
``qwen3_5_context``: the sampling rules no longer match, so the inherited
knobs (``context_frame_count`` spaced by ``chunk_size``, one mutually
exclusive context type per sample) would only be misleading names.

The knobs that replace them:

* ``context_slot_count`` / ``context_slot_stride`` -- 16 slots, 30 timesteps
  apart, i.e. a 16 s visual timeline at the fixed 30 fps this policy assumes.
* ``context_resize_long_side`` -- an optional, smaller long side for the
  context clips only. The current views keep the dataset's resize; a clip at
  long side 192 (128x192 after the processor's 32-pixel snap) costs 24 video
  tokens per span instead of 48 and pushes a third fewer patches through the
  ViT.
* ``demo_probability`` / ``hist_probability`` / ``goal_probability`` -- three
  independent Bernoulli draws, so demo and memory can appear together.
* ``context_view_index`` -- context clips use one camera (the head), while the
  current observation keeps every valid view.

Video tokens are left exactly as Qwen produces them: one 16-frame clip costs
``8 spans x 48 = 384`` tokens. An extra spatial pool on top of the ViT's own
2x2 patch merge was tried and dropped -- a 64x64-pixel token is far coarser
than anything the video encoder was pretrained on, and the prefill it saves is
not worth degrading the native video understanding.
"""

from __future__ import annotations

from dataclasses import dataclass

from lerobot.configs.policies import PreTrainedConfig
from lerobot.policies.qwen3_5_unified_cond.configuration_qwen3_5_unified_cond import (
    Qwen3_5UnifiedCondConfig,
)


@dataclass
class _ContextVideoFieldsMixin:
    """Fields shared verbatim by the policy and dataset configs.

    The first block overrides inherited unified_cond defaults for the
    action-only context experiment; the second block is context-specific.
    """

    # Measured through the real processor at 192x256 (480x640 resized with
    # long_side=256) with the real system message and a long instruction:
    # demo(16) + hist(16) + goal(1) + 3 current views is 1280 tokens.
    # Every training sample is padded to this, so size it for the run (the
    # README table lists 32-slot and 128x192 configurations).
    max_prompt_tokens: int = 1408
    future_first_probability: float = 0.0
    inference_order: str = "action_first"
    predict_future_images: bool = False
    tokenize_state: bool = False
    # RoboTwin carries no subtask annotations, so training always renders
    # "Output: <Action>".  Defaulting to True would make build_inference render
    # "Output: <Subtask, Action>" and silently shift every later token.
    generate_subtask: bool = False

    context_enabled: bool = True
    # Per source.  demo and hist each get their own budget because they can now
    # coexist; a shared one would let a full demo evict the whole history.
    context_slot_count: int = 16
    # Timesteps between slots, deliberately decoupled from chunk_size:
    # 16 x 30 = 480 frames = 16 s at 30 fps.
    context_slot_stride: int = 30
    # Every dataset is treated as 30 fps regardless of its recorded rate; this
    # only feeds the "<x.x seconds>" text Qwen renders between video spans.
    context_fps: float = 30.0
    # Context clips use this single camera.  image0 is the head/exterior view
    # in every dataset schema.
    context_view_index: int = 0
    # Long side for the context clips only; None reuses the dataset's resize
    # so clips and current views share one geometry. The clips are always
    # resized without padding (``resize_to_long_side``), whatever
    # ``image_resize_mode`` says for the current views. 192 turns 480x640 into
    # 144x192, which Qwen's video processor snaps to 128x192, an 8x12 patch
    # grid and 24 tokens per span (verified through the real processor).
    context_resize_long_side: int | None = None
    # Independent Bernoulli draws, so a sample may carry demo and history at
    # once -- or neither.  There is deliberately no "none" probability.
    # Demo needs the dataset's human_demo_root, so it is off by default.
    demo_probability: float = 0.0
    hist_probability: float = 0.5
    goal_probability: float = 0.0
    # Each available current camera view is independently dropped from the
    # prompt with this probability (image0 is restored if every view drops).
    # Context clips are never dropped.
    current_view_dropout_probability: float = 0.0
    # Probability of removing the "Task: ...; " text, drawn independently of
    # the context flags; a sample may end up with neither task nor context.
    task_dropout_probability: float = 0.4
    # Where the meta line (task, control/predict mode, outputs) sits relative
    # to the context clips: "first" (before them) or "last" (after them, right
    # before <|ctx_now|>). 18 of Qwen3.5's 24 layers are gated recurrences that
    # decay over a long clip, and RoPE favors nearby tokens, so "last" keeps
    # the instruction fresh for the action tokens and makes the clip cache
    # task-independent; "first" lets the clip be encoded with the task in
    # view. A layout change, so it needs retraining.
    context_meta_position: str = "first"
    # Language segment ``Memory: ..\nScene: ..\nSubtask: ..`` (fixed order, see LANGUAGE_FIELDS): each
    # annotated field is rendered with its own independent probability, and the prompt's Output list
    # names exactly the rendered ones -- none rendered gives "Output: <Action, ...>", the row an
    # unannotated downstream dataset or a language-free deployment looks like.  Memory is the previous
    # segment's subtask, Scene the segment's scene_caption.  All gated by generate_subtask.
    memory_probability: float = 0.0
    scene_probability: float = 0.0
    subtask_probability: float = 1.0


CONTEXT_META_POSITIONS = ("first", "last")


# Context-only fields configure_for_policy mirrors from the policy config; the
# overridden unified_cond fields are already in the parent's mirrored list.
_CONTEXT_VIDEO_FIELDS = tuple(
    name
    for name in _ContextVideoFieldsMixin.__dataclass_fields__
    if name not in Qwen3_5UnifiedCondConfig.__dataclass_fields__
)


def _validate_context_video_config(config: _ContextVideoFieldsMixin) -> None:
    if config.tokenize_state:
        raise ValueError("qwen3_5_context_video requires tokenize_state=False")
    if config.context_slot_count <= 0:
        raise ValueError("context_slot_count must be positive")
    if config.context_slot_stride <= 0:
        raise ValueError("context_slot_stride must be positive")
    if not float(config.context_fps) > 0.0:
        raise ValueError("context_fps must be positive")
    if config.context_view_index < 0:
        raise ValueError("context_view_index must be non-negative")
    if config.context_view_index >= config.max_num_views:
        raise ValueError("context_view_index must be below max_num_views")
    if config.context_resize_long_side is not None and config.context_resize_long_side <= 0:
        raise ValueError("context_resize_long_side must be positive when set")
    # Deliberately not required to sum to 1: the three draws are independent.
    for name in (
        "demo_probability",
        "hist_probability",
        "goal_probability",
        "current_view_dropout_probability",
        "task_dropout_probability",
        "memory_probability",
        "scene_probability",
        "subtask_probability",
    ):
        value = float(getattr(config, name))
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"{name} must be in [0,1]")
    if not config.context_enabled and config.task_dropout_probability != 0.0:
        raise ValueError("context_enabled=False requires task_dropout_probability=0")
    if config.context_meta_position not in CONTEXT_META_POSITIONS:
        raise ValueError(f"context_meta_position must be one of {CONTEXT_META_POSITIONS}")




@PreTrainedConfig.register_subclass("qwen3_5_context_video")
@dataclass
class Qwen3_5ContextVideoConfig(  # noqa: N801
    _ContextVideoFieldsMixin, Qwen3_5UnifiedCondConfig
):
    def __post_init__(self) -> None:
        super().__post_init__()
        _validate_context_video_config(self)
