#!/usr/bin/env python

"""Prompt construction for the video-packed context prefix.

Layout, per user message:

```text
static meta                       (context_meta_position="first", the default)
<|ctx_demo|> <video: head-camera demo clip>
<|ctx_goal|> <video: single goal frame>
<|ctx_hist|> <video: head-camera history clip>
static meta                       (context_meta_position="last")
<|ctx_now|>  current multi-camera image block
State: <|learned_state|>
```

One ``marker + video`` group per **source** (not per source-and-camera as in
v1): context clips are head-camera only, so a source is a single clip. The
current observation deliberately stays a plain multi-camera image block --
``temporal_patch_size=2`` means appending it to a clip would fuse it with the
last context frame inside one ``Conv3d`` patch, and the current frames are the
ones the model is accused of ignoring.

The processor expands each video into ``ceil(T/2)`` timestamped vision spans,
each holding ``grid_h*grid_w/4`` placeholders. Those counts are left alone --
the video tokens reach the language model exactly as Qwen's own patch merger
produced them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import torch
import torch.nn.functional as F  # noqa: N812
from transformers.video_utils import VideoMetadata

from lerobot.policies.qwen3_5_context_video.tokenization_qwen3_5_context_video import (
    ALL_MARKERS,
    CTX_NOW_TOKEN,
    CTX_NOW_TOKEN_ID,
    LANGUAGE_FIELDS,
    LANGUAGE_GENERATE_KEY,
    LANGUAGE_IDS_KEY,
    LANGUAGE_LENGTH_KEY,
    SOURCE_MARKERS,
    SOURCE_RENDER_ORDER,
    SOURCE_TOKEN_IDS,
    ContextSlot,
    replace_marker_spans,
    sort_context_slots,
)
from lerobot.policies.qwen3_5_unified_cond.video_keys import (
    PIXEL_VALUES_VIDEOS_KEY,
    VIDEO_GRID_THW_KEY,
)
from lerobot.policies.qwen3_5_unified_cond.tokenization_qwen3_5_unified_cond import (
    LEARNED_STATE_TOKEN,
)
from lerobot.policies.qwen3_5_unified_cond.transform_qwen3_5_unified_cond import (
    Qwen3_5UnifiedCondProcessorTransformFn,
)
from lerobot.transforms.constants import SYSTEM_MESSAGE
from lerobot.transforms.core import DataDict, DataTransformFn
from lerobot.utils.constants import OBS_IMAGES, OBS_STR

CONTEXT_SLOTS_KEY = "context.slots"
DROP_TASK_KEY = "context.drop_task"
CURRENT_VIEW_KEEP_KEY = "context.current_view_keep"
# build_inference only: (forced {field: text}, fields the model generates), both in LANGUAGE_FIELDS order.
LANGUAGE_REQUEST_KEY = "context.language_request"


def _scalar_bool(value: Any) -> bool:
    tensor = torch.as_tensor(value, dtype=torch.bool)
    if tensor.numel() != 1:
        raise ValueError("Expected a scalar boolean")
    return bool(tensor.item())


def _bernoulli(probability: float) -> bool:
    """p==0/p==1 skip the RNG entirely, like ``sample_order``."""
    if probability <= 0.0:
        return False
    return probability >= 1.0 or float(torch.rand(()).item()) < probability


def _one_line(text: Any) -> str:
    """Field text on one line: the segment uses newlines as field delimiters."""
    return " ".join(str(text).split())


def _single_chw_image(value: Any) -> torch.Tensor:
    image = torch.as_tensor(value)
    if image.ndim == 4:
        image = image[0]
    if image.ndim != 3:
        raise ValueError("Expected an image of shape [C,H,W]")
    return image


@DataTransformFn.register_subclass("qwen3_5_context_video_processor")
@dataclass
class Qwen3_5ContextVideoProcessorTransformFn(Qwen3_5UnifiedCondProcessorTransformFn):  # noqa: N801
    context_slot_count: int = 16
    context_fps: float = 30.0
    context_view_index: int = 0
    # "first" | "last": where the meta line sits relative to the clips.
    context_meta_position: str = "first"
    # Language segment sources (LoadActionTextFromJsonlTransformFn outputs) and per-field draw
    # probabilities; a field is rendered only when drawn and annotated.  generate_subtask gates them all.
    memory_key: str = "previous_sub_task"
    scene_key: str = "scene_caption"
    memory_probability: float = 0.0
    scene_probability: float = 0.0
    subtask_probability: float = 1.0

    # The inherited sequence builder is the right one: context frames carry no
    # state, so the prefix holds exactly one <|learned_state|> and the base
    # "exactly one" rule applies unchanged.

    def _slots(self, data: DataDict) -> list[ContextSlot]:
        raw_slots = data.get(CONTEXT_SLOTS_KEY, ())
        if not isinstance(raw_slots, Sequence) or isinstance(raw_slots, (str, bytes)):
            raise TypeError(f"{CONTEXT_SLOTS_KEY} must be a sequence")
        return sort_context_slots(raw_slots, max_slots_per_source=self.context_slot_count)

    def _meta_text(self, data: DataDict, *, requested_order: int, language_fields: Sequence[str]) -> str:
        """First prompt line: optional task, control mode, predict mode, and outputs.

        Marker spans are located by forward search, so an instruction containing
        a literal marker would be replaced instead of the real one.  An empty
        task (an episode whose sidecar lists no instruction) renders like a dropped one.
        """
        task = str(data.get(self.task_key, "")).strip()
        if any(marker in task for marker in ALL_MARKERS):
            raise ValueError("Task text contains a reserved context marker")
        predict_mode, outputs = self._prompt_header(
            requested_order=requested_order, language_fields=language_fields
        )
        drop_task = _scalar_bool(data.get(DROP_TASK_KEY, False)) or not task
        task_fragment = "" if drop_task else f"Task: {task}; "
        return (
            f"{task_fragment}Control Mode: <{self.action_mode}>; "
            f"Predict Mode: {predict_mode}; Output: <{', '.join(outputs)}>\n"
        )

    def _clip_content(
        self, slots: Sequence[ContextSlot]
    ) -> tuple[list[dict[str, Any]], list[torch.Tensor], list[VideoMetadata], list[tuple[str, int]]]:
        """One ``marker + video`` group per source, head camera only."""
        content: list[dict[str, Any]] = []
        videos: list[torch.Tensor] = []
        metadata: list[VideoMetadata] = []
        markers: list[tuple[str, int]] = []
        for source in SOURCE_RENDER_ORDER:
            group = [slot for slot in slots if slot.source is source]
            if not group:
                continue
            frame_indices = [slot.frame_index for slot in group]
            if any(index is None for index in frame_indices):
                raise ValueError(f"{source.value} slots need frame_index to render video timestamps")
            content.append({"type": "text", "text": f"{SOURCE_MARKERS[source]} "})
            content.append({"type": "video"})
            content.append({"type": "text", "text": "\n"})
            videos.append(torch.stack([_single_chw_image(slot.images[0]) for slot in group]))
            # A fresh list per clip: the processor mutates frames_indices in
            # place when the frame count is odd.  fps is the policy's fixed
            # rate, not the dataset's -- only relative spacing is meaningful.
            metadata.append(
                VideoMetadata(
                    total_num_frames=len(group),
                    fps=float(self.context_fps),
                    frames_indices=[int(index) for index in frame_indices],
                )
            )
            markers.append((SOURCE_MARKERS[source], SOURCE_TOKEN_IDS[source]))
        return content, videos, metadata, markers

    def _current_images(self, data: DataDict) -> list[torch.Tensor]:
        """Valid current cameras after the optional per-view dropout keep mask."""
        keep_mask = data.get(CURRENT_VIEW_KEEP_KEY)
        if keep_mask is not None:
            keep_mask = torch.as_tensor(keep_mask, dtype=torch.bool).flatten()
            if keep_mask.numel() != self.max_num_views:
                raise ValueError(f"{CURRENT_VIEW_KEEP_KEY} must contain {self.max_num_views} values")
        images = []
        for index in range(self.max_num_views):
            key = f"{OBS_IMAGES}.image{index}"
            keep_view = keep_mask is None or bool(keep_mask[index])
            if key in data and bool(data.get(f"{key}_mask", False)) and keep_view:
                images.append(_single_chw_image(data[key]))
        if not images:
            raise ValueError("qwen3_5_context_video requires at least one valid current camera")
        return images

    def _process_current_context(
        self, data: DataDict, *, requested_order: int, language_fields: Sequence[str]
    ) -> tuple[Any, torch.Tensor]:
        clip_content, videos, metadata, markers = self._clip_content(self._slots(data))
        meta = self._meta_text(data, requested_order=requested_order, language_fields=language_fields)
        if self.context_meta_position not in ("first", "last"):
            raise ValueError(f"Unknown context_meta_position {self.context_meta_position!r}")
        # "last" puts the meta line after the clips; without clips both layouts
        # render the same prompt.
        meta_content: list[dict[str, Any]] = [{"type": "text", "text": meta}]
        if self.context_meta_position == "last":
            content: list[dict[str, Any]] = [*clip_content, *meta_content]
        else:
            content = [*meta_content, *clip_content]

        # Mark the current observation too, so "what follows is now" is a token
        # rather than something the model has to infer from block position.
        content.append({"type": "text", "text": f"{CTX_NOW_TOKEN} "})
        markers.append((CTX_NOW_TOKEN, CTX_NOW_TOKEN_ID))

        current_images = self._current_images(data)
        content.extend({"type": "image"} for _ in current_images)
        content.append({"type": "text", "text": f"\nState: {LEARNED_STATE_TOKEN}"})

        messages = [
            {"role": "system", "content": SYSTEM_MESSAGE},
            {"role": "user", "content": content},
        ]
        prompt = self._processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        video_kwargs: dict[str, Any] = {}
        if videos:
            # Without explicit metadata the processor silently assumes fps=24;
            # without do_sample_frames=False it resamples the clip at 2 fps and
            # throws away the chosen keyframes.
            video_kwargs = {"videos": videos, "video_metadata": metadata, "do_sample_frames": False}
        processed = self._processor(
            text=[prompt],
            images=current_images,
            do_rescale=False,
            return_tensors="pt",
            padding=False,
            truncation=False,
            **video_kwargs,
        )
        #####
        # ids = processed.input_ids[0].tolist()
        # pieces, run, prev = [], 0, None
        # for tid in ids + [None]:
        #     if tid == prev:
        #         run += 1
        #         continue
        #     if prev is not None:
        #         tok = self._tokenizer.decode([prev])
        #         pieces.append(f"{tok}x{run}" if run > 1 else tok)
        #     prev, run = tid, 1
        # print("expanded prompt:", "".join(pieces))
        #####
        prefix_ids = replace_marker_spans(
            processed.input_ids[0], tokenizer=self._tokenizer, markers=markers
        )
        return processed, prefix_ids

    def _model_inputs_head(
        self, *, state: torch.Tensor, processed: Any, sequence: dict[str, torch.Tensor]
    ) -> DataDict:
        """Always export the video fields so the collate key set is batch-stable."""
        output = super()._model_inputs_head(state=state, processed=processed, sequence=sequence)
        pixel_values = output[f"{OBS_STR}.pixel_values"]
        pixel_values_videos = processed.get("pixel_values_videos")
        video_grid_thw = processed.get("video_grid_thw")
        if pixel_values_videos is None:
            pixel_values_videos = pixel_values.new_zeros((0, pixel_values.shape[-1]))
            video_grid_thw = torch.zeros((0, 3), dtype=torch.long)
        output[PIXEL_VALUES_VIDEOS_KEY] = pixel_values_videos
        output[VIDEO_GRID_THW_KEY] = torch.as_tensor(video_grid_thw).long()
        return output

    def _encode_language(self, fields: Mapping[str, str], *, open_field: str | None = None) -> torch.Tensor:
        """Segment body ids: ``Name: text`` per field, newline-joined.

        Every piece (label, text, newline) is tokenized alone, so BPE never
        merges across a field boundary and a prefix forced at inference is
        id-identical to the training body.  ``open_field`` appends the newline
        and the label of the first field the model is left to decode.
        """
        pieces: list[str] = []
        names = list(fields)
        for position, name in enumerate(names):
            pieces.extend((f"{name}:", f" {fields[name]}"))
            if position + 1 < len(names) or open_field is not None:
                pieces.append("\n")
        if open_field is not None:
            pieces.append(f"{open_field}:")
        ids = [token for piece in pieces for token in self._tokenizer(piece, add_special_tokens=False).input_ids]
        return torch.tensor(ids, dtype=torch.long)

    def _language_segment(self, data: DataDict) -> tuple[tuple[str, ...], torch.Tensor]:
        """Each annotated field is drawn independently; none drawn gives the action-only header."""
        if not self.generate_subtask:
            return (), torch.empty(0, dtype=torch.long)
        keys = {"Memory": self.memory_key, "Scene": self.scene_key, "Subtask": self.subtask_key}
        probabilities = {
            "Memory": self.memory_probability, "Scene": self.scene_probability, "Subtask": self.subtask_probability
        }
        fields = {}
        for name in LANGUAGE_FIELDS:
            text = _one_line(data.get(keys[name], ""))
            if text and _bernoulli(probabilities[name]):
                fields[name] = text
        return tuple(fields), self._encode_language(fields)

    def _inference_language_fields(self, data: DataDict) -> tuple[str, ...]:
        forced, generate = data[LANGUAGE_REQUEST_KEY]
        return (*forced, *generate)

    def build_inference(
        self,
        data: DataDict,
        *,
        contexts: Sequence[ContextSlot | Mapping[str, Any]] | None = None,
        include_task: bool = True,
        inference_order: str | None = None,
        view_indices: tuple[int, ...] | None = None,
        language: Mapping[str, str] | None = None,
        generate: Sequence[str] | None = None,
    ) -> DataDict:
        """``language`` holds the fields given by the caller (``{"Memory": ..., "Subtask": ...}``), ``generate``
        the fields the model decodes after them; the prompt announces both.  ``generate=None`` means
        ``("Subtask",)`` when generate_subtask is on and the caller did not give one, else nothing.
        Forced fields must all precede the generated ones in LANGUAGE_FIELDS order."""
        forced = {name: _one_line(text) for name, text in (language or {}).items()}
        if generate is None:
            generate = ("Subtask",) if self.generate_subtask and "Subtask" not in forced else ()
        unknown = (set(forced) | set(generate)) - set(LANGUAGE_FIELDS)
        if unknown:
            raise ValueError(f"Unknown language fields {sorted(unknown)}; expected a subset of {LANGUAGE_FIELDS}")
        if set(forced) & set(generate) or len(set(generate)) != len(generate):
            raise ValueError("A language field is either given or generated, once")
        if any(not text for text in forced.values()):
            raise ValueError("A given language field needs non-empty text")
        forced = {name: forced[name] for name in LANGUAGE_FIELDS if name in forced}
        generate = tuple(name for name in LANGUAGE_FIELDS if name in generate)
        if forced and generate and LANGUAGE_FIELDS.index(list(forced)[-1]) > LANGUAGE_FIELDS.index(generate[0]):
            raise ValueError(f"Given fields must precede generated ones in {LANGUAGE_FIELDS} order")

        inference_data = dict(data)
        inference_data[CONTEXT_SLOTS_KEY] = list(contexts or ())
        inference_data[DROP_TASK_KEY] = not include_task
        inference_data[LANGUAGE_REQUEST_KEY] = (forced, generate)
        output = super().build_inference(
            inference_data, inference_order=inference_order, view_indices=view_indices
        )
        ids = self._encode_language(forced, open_field=generate[0] if generate else None)
        if ids.numel() > self.max_subtask_tokens:
            raise ValueError(f"Given language fields take {ids.numel()} tokens, over max_subtask_tokens")
        output[LANGUAGE_IDS_KEY] = F.pad(ids, (0, self.max_subtask_tokens - ids.numel()))
        output[LANGUAGE_LENGTH_KEY] = torch.tensor(ids.numel(), dtype=torch.long)
        output[LANGUAGE_GENERATE_KEY] = torch.tensor(bool(generate))
        return output
