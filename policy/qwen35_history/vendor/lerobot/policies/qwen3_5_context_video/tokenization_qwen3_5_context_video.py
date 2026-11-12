#!/usr/bin/env python

"""Token contract for the video-packed context prefix.

Self-contained on purpose: v2 of this policy no longer derives from
``qwen3_5_context``, because the two now differ in what a context slot *is*
(16 head-camera frames one ``context_slot_stride`` apart vs 4 multi-camera
frames one action chunk apart).  Sharing the sampler would only make the
divergence harder to read, so the small pieces that stay identical -- the
sparse marker ids and their rendered text -- are duplicated here instead.

One thing is specific to this policy: ``<|ctx_now|>``, a fourth input-only id
opening the current observation, so "what follows is now" is a token rather
than something inferred from block position.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

import torch

from lerobot.policies.qwen3_5_unified_cond.tokenization_qwen3_5_unified_cond import IBQ_TOKEN_END

# v6 was the front-layout image prefix with per-frame states; v7 packed context
# frames into per-camera video blocks; v8 is one head-camera clip per source,
# 16 slots one stride apart.
CONTEXT_VIDEO_TOKEN_SCHEMA_VERSION = 8

CTX_TOKEN_START = IBQ_TOKEN_END
CTX_HIST_TOKEN_ID = CTX_TOKEN_START
CTX_DEMO_TOKEN_ID = CTX_TOKEN_START + 1
CTX_GOAL_TOKEN_ID = CTX_TOKEN_START + 2
CTX_NOW_TOKEN_ID = CTX_TOKEN_START + 3
CTX_VIDEO_TOKEN_END = CTX_TOKEN_START + 4

CTX_HIST_TOKEN = "<|ctx_hist|>"
CTX_DEMO_TOKEN = "<|ctx_demo|>"
CTX_GOAL_TOKEN = "<|ctx_goal|>"
CTX_NOW_TOKEN = "<|ctx_now|>"


class ContextSource(str, Enum):
    HIST = "hist"
    DEMO = "demo"
    GOAL = "goal"


SOURCE_ORDER = {
    ContextSource.DEMO: 0,
    ContextSource.GOAL: 1,
    ContextSource.HIST: 2,
}
SOURCE_MARKERS = {
    ContextSource.HIST: CTX_HIST_TOKEN,
    ContextSource.DEMO: CTX_DEMO_TOKEN,
    ContextSource.GOAL: CTX_GOAL_TOKEN,
}
SOURCE_TOKEN_IDS = {
    ContextSource.HIST: CTX_HIST_TOKEN_ID,
    ContextSource.DEMO: CTX_DEMO_TOKEN_ID,
    ContextSource.GOAL: CTX_GOAL_TOKEN_ID,
}
# Clips are emitted in this order; ``sort_context_slots`` uses the same key.
SOURCE_RENDER_ORDER = tuple(sorted(SOURCE_ORDER, key=SOURCE_ORDER.__getitem__))

ALL_MARKERS = (*SOURCE_MARKERS.values(), CTX_NOW_TOKEN)

# The language segment ``<subtask_start>Memory: ..\nScene: ..\nSubtask: ..<subtask_end>`` holds any subset
# of these fields in this order: what is known (the last finished subtask) before what is inferred, so a
# caller (a System 2, the deploy loop) can force any prefix of the fields and let the model decode the rest.
# The whole body is one text CE (``LOSS_SUBTASK``, logged as ``loss_text`` by the KI policy).
LANGUAGE_FIELDS = ("Memory", "Scene", "Subtask")
# build_inference -> model: the forced segment body (zero-padded to max_subtask_tokens), its length, and
# whether the model decodes the remaining fields.  The model opens the segment itself.
LANGUAGE_IDS_KEY = "observation.language_ids"
LANGUAGE_LENGTH_KEY = "observation.language_length"
LANGUAGE_GENERATE_KEY = "observation.language_generate"


def parse_language_segment(text: str) -> dict[str, str]:
    """Inverse of the segment rendering: ``Name: text`` lines -> ``{name: text}``, unknown lines dropped."""
    fields: dict[str, str] = {}
    for line in text.split("\n"):
        name, separator, value = line.partition(":")
        if separator and name.strip() in LANGUAGE_FIELDS:
            fields[name.strip()] = value.strip()
    return fields


@dataclass(frozen=True)
class ContextSlot:
    """One context frame.

    ``images`` is a tuple of per-view ``[C,H,W]`` tensors purely for shape
    compatibility with the eval helpers; this policy only ever renders
    ``images[0]``, the head camera.  Context frames carry no state, so the
    prefix keeps the inherited "exactly one ``<|learned_state|>``" rule.
    """

    images: tuple[Any, ...]
    source: ContextSource
    # Episode-relative index, used to render honest ``<x.x seconds>`` markers.
    frame_index: int | None = None

    def __post_init__(self) -> None:
        if isinstance(self.images, torch.Tensor):
            raise TypeError("images must be a sequence of per-view [C,H,W] tensors, not one tensor")
        object.__setattr__(self, "images", tuple(self.images))
        if not self.images:
            raise ValueError("A context slot needs at least one view image")


def coerce_context_slot(value: ContextSlot | Mapping[str, Any]) -> ContextSlot:
    if isinstance(value, ContextSlot):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("Each context must be a ContextSlot or mapping")
    missing = {"images", "source"}.difference(value)
    if missing:
        raise KeyError(f"Context slot is missing fields: {sorted(missing)}")
    raw_source = value["source"]
    try:
        source = raw_source if isinstance(raw_source, ContextSource) else ContextSource(str(raw_source))
    except ValueError as error:
        raise ValueError(f"Unknown context source: {value['source']!r}") from error
    return ContextSlot(
        images=value["images"], source=source, frame_index=value.get("frame_index")
    )


def sort_context_slots(
    values: Sequence[ContextSlot | Mapping[str, Any]], *, max_slots_per_source: int
) -> list[ContextSlot]:
    """Group by source (demo -> goal -> hist) and cap each source independently.

    Sources are capped separately because demo and history can now coexist in
    one prompt; a shared budget would let a full demo silently evict the whole
    history.  Within a source the caller's order is preserved -- slots arrive
    oldest-first, so an over-long history drops its head (the oldest frames).
    """

    if max_slots_per_source <= 0:
        raise ValueError("max_slots_per_source must be positive")
    slots = [coerce_context_slot(value) for value in values]
    kept: list[ContextSlot] = []
    for source in SOURCE_RENDER_ORDER:
        group = [slot for slot in slots if slot.source is source]
        kept.extend(group[len(group) - max_slots_per_source :] if len(group) > max_slots_per_source else group)
    return kept


def replace_marker_spans(
    input_ids: torch.Tensor, *, tokenizer: Any, markers: Sequence[tuple[str, int]]
) -> torch.Tensor:
    """Replace rendered marker text spans with sparse model-global ids.

    Keyed on explicit ``(text, id)`` pairs rather than on ``ContextSource``
    because this policy also marks the current observation, which is not a
    source.  Spans are located by forward search, so ``markers`` must list the
    markers in the order they were rendered.
    """

    ids = torch.as_tensor(input_ids, dtype=torch.long).flatten().tolist()
    if any(token_id >= CTX_TOKEN_START for token_id in ids):
        raise ValueError("input_ids already contain context marker ids")
    output: list[int] = []
    cursor = 0
    for text, token_id in markers:
        needle = tokenizer(text, add_special_tokens=False).input_ids
        if not needle:
            raise ValueError(f"Tokenizer produced an empty marker span for {text}")
        start = next(
            (
                index
                for index in range(cursor, len(ids) - len(needle) + 1)
                if ids[index : index + len(needle)] == needle
            ),
            None,
        )
        if start is None:
            raise ValueError(f"Rendered prompt is missing marker {text}")
        output.extend(ids[cursor:start])
        output.append(token_id)
        cursor = start + len(needle)
    output.extend(ids[cursor:])
    return torch.tensor(output, dtype=torch.long, device=input_ids.device)


__all__ = [
    "ALL_MARKERS",
    "CONTEXT_VIDEO_TOKEN_SCHEMA_VERSION",
    "CTX_DEMO_TOKEN",
    "CTX_DEMO_TOKEN_ID",
    "CTX_GOAL_TOKEN",
    "CTX_GOAL_TOKEN_ID",
    "CTX_HIST_TOKEN",
    "CTX_HIST_TOKEN_ID",
    "CTX_NOW_TOKEN",
    "CTX_NOW_TOKEN_ID",
    "CTX_TOKEN_START",
    "CTX_VIDEO_TOKEN_END",
    "ContextSlot",
    "ContextSource",
    "SOURCE_MARKERS",
    "SOURCE_ORDER",
    "SOURCE_RENDER_ORDER",
    "SOURCE_TOKEN_IDS",
    "coerce_context_slot",
    "replace_marker_spans",
    "sort_context_slots",
]
