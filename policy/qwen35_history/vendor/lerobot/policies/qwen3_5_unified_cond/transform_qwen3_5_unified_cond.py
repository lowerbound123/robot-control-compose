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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import cache
from typing import Any

import numpy as np
import torch
import torch.nn.functional as F  # noqa: N812
from transformers.models.qwen3_5 import Qwen3_5Tokenizer
from transformers.models.qwen3_vl import Qwen3VLProcessor

from lerobot.dataset_schemas import get_schema
from lerobot.policies.qwen3_5_unified_cond.ibq_codec import IBQ_DOWNSAMPLE_FACTOR
from lerobot.policies.qwen3_5_unified_cond.tokenization_qwen3_5_unified_cond import (
    ACTION_IMAGE_TOKEN,
    DEFAULT_FAST_TOKENIZER_NAME,
    DEFAULT_MAX_FUTURE_GRID_HW,
    FUTURE_VIEW_SAMPLING_ALL,
    IBQ_KIND_COND,
    IBQ_KIND_FUTURE,
    IMAGE_ACTION_TOKEN,
    LEARNED_STATE_TOKEN,
    ORDER_ACTION_FIRST,
    ORDER_FUTURE_FIRST,
    UnifiedSequenceBuilder,
    ensure_unified_special_tokens,
    ibq_code_ids_to_global_ids,
    order_from_name,
    resolve_local_snapshot,
    sample_future_views,
    sample_order,
)
from lerobot.transforms.constants import SYSTEM_MESSAGE
from lerobot.transforms.core import DataDict, DataTransformFn
from lerobot.transforms.utils import pad_to_multiple, resize_with_pad
from lerobot.utils.constants import ACTION, OBS_IMAGES, OBS_STATE, OBS_STR

FUTURE_GRID_HW_KEY = "observation.future_frame.grid_hw"
FAST_TOKEN_KEY = "action.fast_tokens"
REQUESTED_ORDER_KEY = "unified.requested_order"
FUTURE_VIEW_INDICES_KEY = "unified.future_view_indices"
IBQ_KIND_KEY = "unified.ibq_kind"
IBQ_COORDS_KEY = "unified.ibq_coords"
COND_CACHED_KEY = "unified.cond_cached"


def future_image_key(index: int) -> str:
    return f"observation.future_frame.image{index}"


def future_image_mask_key(index: int) -> str:
    return f"{future_image_key(index)}_mask"


def future_token_cache_key(index: int) -> str:
    """Cache key containing codec-local, never model-global, IBQ code ids."""
    return f"{future_image_key(index)}.ibq_code_ids"


def cond_image_key(index: int) -> str:
    """The current frame of camera ``index``, kept in [0,1] for the IBQ codec."""
    return f"observation.cond_frame.image{index}"


def cond_token_cache_key(index: int) -> str:
    return f"{cond_image_key(index)}.ibq_code_ids"


# The IBQ frames a sample carries for online tokenization, packed the way Qwen packs
# ``pixel_values`` so frames of any size collate by concatenation: every frame's pixels as
# ``[sum(H*W), 3]`` rows in raster order, one ``(kind, camera, H, W)`` row per frame and the
# frame count per sample. A sample without frames (VQA) packs to zero rows.
IBQ_FRAMES_KEY = "unified.ibq_frames"
IBQ_FRAME_META_KEY = "unified.ibq_frame_meta"
IBQ_FRAME_COUNT_KEY = "unified.ibq_frame_count"


def pack_ibq_frames(frames: Sequence[tuple[int, int, torch.Tensor]]) -> DataDict:
    """Pack ``(kind, camera, [3,H,W] frame)`` triples; ``kind`` is the body kind the frame fills."""
    pixels, meta = [], []
    for kind, camera, frame in frames:
        frame = torch.as_tensor(frame)
        if frame.ndim != 3 or frame.shape[0] != 3:
            raise ValueError(f"IBQ frames must be [3,H,W], got {tuple(frame.shape)}")
        pixels.append(frame.permute(1, 2, 0).reshape(-1, 3).to(torch.float32))
        meta.append((int(kind), int(camera), int(frame.shape[1]), int(frame.shape[2])))
    return {
        IBQ_FRAMES_KEY: torch.cat(pixels) if pixels else torch.zeros((0, 3), dtype=torch.float32),
        IBQ_FRAME_META_KEY: torch.tensor(meta, dtype=torch.long).reshape(-1, 4),
        IBQ_FRAME_COUNT_KEY: torch.tensor(len(meta), dtype=torch.long),
    }


def unpack_ibq_frames(
    batch: Mapping[str, Any], *, num_rows: int | None = None
) -> list[dict[tuple[int, int], torch.Tensor]]:
    """Per batch row, ``{(kind, camera): [3,H,W]}``; a single sample (scalar count) is one row.

    With ``num_rows`` given, a batch without the packed keys simply carries no frames (the
    model raises only when a body needs one); without it the keys are required.
    """
    if IBQ_FRAME_COUNT_KEY not in batch and num_rows is not None:
        return [{} for _ in range(num_rows)]
    pixels = torch.as_tensor(batch[IBQ_FRAMES_KEY]).reshape(-1, 3)
    meta = torch.as_tensor(batch[IBQ_FRAME_META_KEY]).reshape(-1, 4).tolist()
    counts = torch.as_tensor(batch[IBQ_FRAME_COUNT_KEY]).reshape(-1).tolist()
    if sum(counts) != len(meta) or sum(height * width for _, _, height, width in meta) != pixels.shape[0]:
        raise ValueError("Packed IBQ frames, frame meta and frame counts disagree")
    rows: list[dict[tuple[int, int], torch.Tensor]] = []
    offset = frame_index = 0
    for count in counts:
        row: dict[tuple[int, int], torch.Tensor] = {}
        for _ in range(count):
            kind, camera, height, width = meta[frame_index]
            row[(kind, camera)] = pixels[offset : offset + height * width].reshape(height, width, 3).permute(2, 0, 1)
            offset += height * width
            frame_index += 1
        rows.append(row)
    return rows


def _images_grid_hw(images: Sequence[torch.Tensor]) -> torch.Tensor:
    """IBQ token-grid [V,2] (H/16, W/16) of the given CHW images."""
    return torch.tensor(
        [
            (image.shape[-2] // IBQ_DOWNSAMPLE_FACTOR, image.shape[-1] // IBQ_DOWNSAMPLE_FACTOR)
            for image in images
        ],
        dtype=torch.long,
    )


def available_view_indices(
    dataset: LeRobotDataset | StreamingLeRobotDataset, candidate_indices: tuple[int, ...]
) -> tuple[int, ...]:
    """Keep the candidate cameras this dataset actually has."""
    canonical_keys = set(get_schema(dataset.meta.robot_type).image_mapping.values())
    return tuple(index for index in candidate_indices if f"{OBS_IMAGES}.image{index}" in canonical_keys)


@cache
def _load_processor(snapshot: str) -> tuple[Qwen3VLProcessor, Qwen3_5Tokenizer, Any]:
    """Once per process: each repo hydrates its own transform copy, and a rank can hold hundreds of repos."""
    processor = Qwen3VLProcessor.from_pretrained(snapshot, local_files_only=True)
    tokenizer = Qwen3_5Tokenizer.from_pretrained(snapshot, local_files_only=True)
    processor.tokenizer = tokenizer
    return processor, tokenizer, ensure_unified_special_tokens(tokenizer)


@DataTransformFn.register_subclass("qwen3_5_unified_cond_extract_future_images")
@dataclass
class ExtractFutureImagesTransformFn(DataTransformFn):
    """Split candidate, already-resized ``[t, t+H]`` camera tensors, then sample views.

    Every available candidate camera gets its own future field in the data dict,
    while ``future_view_sampling`` decides which of them this sample really
    predicts; only those travel with the sample (see ``pack_ibq_frames``). The
    draw is recorded for the processor, so one draw drives both.
    """

    future_image_indices: tuple[int, ...] = (0,)
    future_view_sampling: str = FUTURE_VIEW_SAMPLING_ALL
    source_image_keys: tuple[str, ...] = ()

    def hydrate(self, dataset: LeRobotDataset | StreamingLeRobotDataset) -> ExtractFutureImagesTransformFn:
        image_mapping = get_schema(dataset.meta.robot_type).image_mapping
        candidates = available_view_indices(dataset, self.future_image_indices)
        source_keys = tuple(
            next(
                source_key
                for source_key, canonical_key in image_mapping.items()
                if canonical_key == f"{OBS_IMAGES}.image{index}"
            )
            for index in candidates
        )
        return replace(self, future_image_indices=candidates, source_image_keys=source_keys)

    def __call__(self, data: DataDict) -> DataDict:
        """Unpack each candidate camera's [t, t+H] pair into current + future keys."""
        source_keys = self.source_image_keys or tuple(
            f"{OBS_IMAGES}.image{index}" for index in self.future_image_indices
        )
        for index, source_key in zip(self.future_image_indices, source_keys, strict=True):
            current, future = data[source_key]
            data[source_key] = current
            data[future_image_key(index)] = future
            # Out-of-episode future frames are clamped to the last episode frame
            # by delta sampling and used as real targets (matching qwen3_5_fast's
            # tail padding), so the mask is always true; the key stays for the
            # collated schema and downstream consumers.
            data[future_image_mask_key(index)] = torch.tensor(True)
        data[FUTURE_VIEW_INDICES_KEY] = torch.tensor(
            sample_future_views(self.future_image_indices, mode=self.future_view_sampling)
        )
        return data




@DataTransformFn.register_subclass("qwen3_5_unified_cond_processor")
@dataclass
class Qwen3_5UnifiedCondProcessorTransformFn(DataTransformFn):  # noqa: N801
    """Compose the Qwen prompt and unified token sequence into model inputs.

    ``__call__`` is the training path (targets included); ``build_inference``
    is the explicit inference path (prefix only, no target fields).
    """

    vlm_model_name_or_path: str = "Qwen/Qwen3.5-2B"
    max_prompt_tokens: int = 350
    max_subtask_tokens: int = 64
    max_action_tokens: int = 256
    max_state_dim: int = 32
    future_first_probability: float = 0.5
    inference_order: str = "future_first"
    generate_subtask: bool = True
    future_image_indices: tuple[int, ...] = (0,)
    # The candidates this dataset really has (hydrate); () means all of them. Only
    # these are sampled, but every candidate is exported so datasets with fewer
    # cameras (A1's Franka: image0/1) collate with three-camera ones.
    available_view_indices: tuple[int, ...] = ()
    max_future_grid_hw: tuple[int, int] = DEFAULT_MAX_FUTURE_GRID_HW
    # "long_side": the cond/future frames are the resize transform's output, zero-padded
    # on the bottom/right up to multiples of the IBQ patch size (16);
    # "pad": they are padded to ibq_image_resolution (the ViT input is untouched).
    ibq_image_resize_mode: str = "long_side"
    ibq_image_resolution: tuple[int, int] = (256, 256)
    max_num_views: int = 3
    max_predicted_views: int = 1
    cond_frame_enabled: bool = True
    future_view_sampling: str = FUTURE_VIEW_SAMPLING_ALL
    predict_future_images: bool = True
    tokenize_state: bool = False
    task_key: str = "task"
    subtask_key: str = "sub_task"
    action_mode: str = "joint"
    _processor: Any | None = field(default=None, init=False, repr=False, compare=False)
    _tokenizer: Any | None = field(default=None, init=False, repr=False, compare=False)
    _builder: UnifiedSequenceBuilder | None = field(default=None, init=False, repr=False, compare=False)

    def hydrate(
        self, dataset: LeRobotDataset | StreamingLeRobotDataset
    ) -> Qwen3_5UnifiedCondProcessorTransformFn:
        schema = get_schema(dataset.meta.robot_type)
        hydrated = replace(
            self,
            action_mode=getattr(schema, "action_mode", "joint"),
            available_view_indices=available_view_indices(dataset, self.future_image_indices),
        )
        hydrated._ensure_loaded()
        return hydrated

    def _sampled_candidates(self) -> tuple[int, ...]:
        return self.available_view_indices or self.future_image_indices

    def _ensure_loaded(self) -> None:
        """Lazily load the VL processor + tokenizer and build the sequence builder."""
        if self._processor is not None:
            return
        snapshot = resolve_local_snapshot(self.vlm_model_name_or_path)
        self._processor, self._tokenizer, special_ids = _load_processor(str(snapshot))
        self._builder = UnifiedSequenceBuilder(
            special_tokens=special_ids,
            max_prompt_tokens=self.max_prompt_tokens,
            max_subtask_tokens=self.max_subtask_tokens,
            max_action_tokens=self.max_action_tokens,
            future_image_indices=self.future_image_indices,
            max_future_grid_hw=self.max_future_grid_hw,
            max_num_views=self.max_num_views,
            max_predicted_views=self.max_predicted_views,
            cond_frame_enabled=self.cond_frame_enabled,
            predict_future_images=self.predict_future_images,
            tokenize_state=self.tokenize_state,
        )

    def _encode_state_text(self, data: DataDict) -> str:
        """KI-style discretized state text; mirrors qwen3_5vla_ki's _encode_state.

        The normalized state is divided by 3 (so +-3 sigma spans the bin range)
        and digitized into 256 bins over [-1, 1].
        """

        state = torch.as_tensor(data[OBS_STATE]).flatten().float()
        if state.shape[0] < self.max_state_dim:
            state = F.pad(state, (0, self.max_state_dim - state.shape[0]))
        state_np = state.cpu().numpy() / 3
        discretized = np.digitize(state_np, bins=np.linspace(-1, 1, 257)[:-1]) - 1
        return "State: " + " ".join(map(str, discretized))

    def _tokenize_subtask(self, data: DataDict) -> torch.Tensor:
        """Tokenize the optional subtask text; empty tensor when absent."""
        subtask = str(data.get(self.subtask_key, "")).strip()
        if not subtask:
            return torch.empty(0, dtype=torch.long)
        encoded = self._tokenizer(subtask, add_special_tokens=False, return_tensors="pt")
        return encoded.input_ids[0]

    def _language_segment(self, data: DataDict) -> tuple[tuple[str, ...], torch.Tensor]:
        """Output fields the segment carries and its body ids (both empty if none).

        generate_subtask gates training too, so the action context the model
        trains on matches what build_inference produces at deployment.
        """
        subtask_ids = self._tokenize_subtask(data) if self.generate_subtask else torch.empty(0, dtype=torch.long)
        fields = ("Subtask",) if subtask_ids.numel() > 0 else ()
        return fields, subtask_ids

    def _inference_language_fields(self, data: DataDict) -> tuple[str, ...]:
        """Output fields build_inference announces; subclasses read an explicit request instead."""
        return ("Subtask",) if self.generate_subtask else ()

    def _prompt_header(self, *, requested_order: int, language_fields: Sequence[str]) -> tuple[str, list[str]]:
        """The predict-mode token and Output list announced in the prompt; language fields come first."""

        future_output = "Future Image" if len(self.future_image_indices) == 1 else "Future Images"
        if requested_order == ORDER_FUTURE_FIRST:
            predict_mode = IMAGE_ACTION_TOKEN
            outputs = [future_output, "Action"]
        elif requested_order == ORDER_ACTION_FIRST:
            predict_mode = ACTION_IMAGE_TOKEN
            outputs = ["Action", future_output]
        else:
            raise ValueError(f"Unknown order id: {requested_order}")
        if not self.predict_future_images:
            if requested_order != ORDER_ACTION_FIRST:
                raise ValueError("predict_future_images=False requires the action_first order")
            outputs.remove(future_output)
        return predict_mode, [*language_fields, *outputs]

    def _process_current_context(
        self, data: DataDict, *, requested_order: int, language_fields: Sequence[str]
    ) -> tuple[Any, torch.Tensor]:
        """Build the Qwen visual/text prefix, like WMVLAChatProcessor."""

        images = []
        for index in range(self.max_num_views):
            key = f"{OBS_IMAGES}.image{index}"
            if key in data and bool(data.get(f"{key}_mask", False)):
                image = data[key]
                if image.ndim == 4:
                    image = image[0]
                images.append(image)
        if not images:
            raise ValueError("qwen3_5_unified_cond requires at least one valid current camera")

        task = str(data.get(self.task_key, "")).strip()
        predict_mode, outputs = self._prompt_header(
            requested_order=requested_order, language_fields=language_fields
        )
        state_repr = self._encode_state_text(data) if self.tokenize_state else f"State: {LEARNED_STATE_TOKEN}"
        user_text = (
            f"Task: {task}; Control Mode: <{self.action_mode}>; "
            f"{state_repr}; Predict Mode: {predict_mode}; "
            f"Output: <{', '.join(outputs)}>"
        )
        messages = [
            {"role": "system", "content": SYSTEM_MESSAGE},
            {
                "role": "user",
                "content": [*({"type": "image"} for _ in images), {"type": "text", "text": user_text}],
            },
        ]
        prompt = self._processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        processed = self._processor(
            text=[prompt],
            images=images,
            do_rescale=False,
            return_tensors="pt",
            padding=False,
            truncation=False,
        )
        return processed, processed.input_ids[0]

    def build_inference(
        self,
        data: DataDict,
        *,
        inference_order: str | None = None,
        view_indices: tuple[int, ...] | None = None,
    ) -> DataDict:
        """Explicit inference path returning model inputs and no target fields.

        Callers first apply the same current-camera resize, key remap
        and state normalization used by the dataset pipeline. State padding is
        done here. This method intentionally is not selected from ``__call__``
        by inspecting optional data-dict keys.
        """

        self._ensure_loaded()
        if inference_order is None:
            inference_order = self.inference_order
        requested_order = order_from_name(inference_order)
        if view_indices is None:
            view_indices = self._sampled_candidates()[: self.max_predicted_views]
        missing_views = [
            index for index in view_indices if not bool(data.get(f"{OBS_IMAGES}.image{index}_mask", False))
        ]
        if missing_views:
            raise ValueError(f"Selected future views are unavailable: {missing_views}")
        processed, prefix_ids = self._process_current_context(
            data, requested_order=requested_order, language_fields=self._inference_language_fields(data)
        )
        cond_frames = self._cond_frames(data, view_indices)
        sequence = self._builder.build_inference_prefix(
            prefix_input_ids=prefix_ids,
            requested_order=requested_order,
            future_grid_hw=_images_grid_hw([frame for _, _, frame in cond_frames]),
            view_indices=view_indices,
        )
        state = torch.as_tensor(data[OBS_STATE])
        if state.ndim != 1 or state.shape[0] > self.max_state_dim:
            raise ValueError(f"Expected one state vector with at most {self.max_state_dim} values")
        if state.shape[0] < self.max_state_dim:
            state = F.pad(state, (0, self.max_state_dim - state.shape[0]))
        output = self._model_inputs_head(state=state, processed=processed, sequence=sequence)
        output.update(self._extra_model_inputs(data))
        output.update(pack_ibq_frames(cond_frames))
        return output

    def __call__(self, data: DataDict) -> DataDict:
        self._ensure_loaded()
        requested_order_value = data.get(REQUESTED_ORDER_KEY)
        requested_order = (
            int(torch.as_tensor(requested_order_value).item())
            if requested_order_value is not None
            else sample_order(self.future_first_probability)
        )
        language_fields, subtask_input_ids = self._language_segment(data)
        processed, prefix_ids = self._process_current_context(
            data, requested_order=requested_order, language_fields=language_fields
        )
        view_indices = self._predicted_views(data)
        # Only the sampled views travel with the sample (packed, so sizes may differ across
        # samples); the sequence holds those views only, and the model encodes nothing else.
        future_frames = [self._ibq_frame(data[future_image_key(index)]) for index in view_indices]
        sequence = self._builder.build(
            prefix_input_ids=prefix_ids,
            fast_ids=data[FAST_TOKEN_KEY],
            subtask_input_ids=subtask_input_ids,
            requested_order=requested_order,
            future_token_ids=self._cached_global_ids(data, future_token_cache_key, view_indices),
            future_grid_hw=_images_grid_hw(future_frames),
            cond_token_ids=self._cached_global_ids(data, cond_token_cache_key, view_indices),
            view_indices=view_indices,
        )
        output = self._model_inputs_head(state=data[OBS_STATE], processed=processed, sequence=sequence)
        output.update(self._extra_model_inputs(data))
        output["labels"] = sequence["labels"]
        output["labels.loss_kind"] = sequence["loss_kind"]
        # print("[SEQ]", self._tokenizer.decode(sequence["input_ids"][sequence["attention_mask"].bool()]).replace("<|image_pad|>", "").replace("<|video_pad|>", ""), "\n[TARGET text]", self._tokenizer.decode(sequence["labels"][sequence["loss_kind"] == 1]), "\n[loss_kind counts none/text/action/future/vqa]", torch.bincount(sequence["loss_kind"].long(), minlength=5).tolist(), flush=True)
        frames: list[tuple[int, int, torch.Tensor]] = []
        if self.predict_future_images:
            if self.cond_frame_enabled:
                frames.extend(self._cond_frames(data, view_indices))
            frames.extend((IBQ_KIND_FUTURE, index, frame) for index, frame in zip(view_indices, future_frames, strict=True))
        output.update(pack_ibq_frames(frames))
        return output

    def _predicted_views(self, data: DataDict) -> tuple[int, ...]:
        """The views sampled upstream, or a fresh draw when running standalone."""
        sampled = data.get(FUTURE_VIEW_INDICES_KEY)
        if sampled is None:
            return sample_future_views(self._sampled_candidates(), mode=self.future_view_sampling)
        return tuple(int(index) for index in torch.as_tensor(sampled).flatten())

    def _cached_global_ids(
        self, data: DataDict, cache_key: Any, view_indices: tuple[int, ...]
    ) -> tuple[torch.Tensor | None, ...]:
        """Read optional pre-encoded IBQ codes and lift them to model-global ids."""
        code_ids = (data.get(cache_key(index)) for index in view_indices)
        return tuple(None if codes is None else ibq_code_ids_to_global_ids(codes) for codes in code_ids)

    def _ibq_frame(self, image: torch.Tensor) -> torch.Tensor:
        """The IBQ codec's view of a resized frame, still in [0,1]; see ``ibq_image_resize_mode``."""
        if self.ibq_image_resize_mode == "long_side":
            return pad_to_multiple(torch.as_tensor(image), IBQ_DOWNSAMPLE_FACTOR)
        if self.ibq_image_resize_mode == "pad":
            return resize_with_pad(torch.as_tensor(image), *self.ibq_image_resolution)
        raise ValueError(f"Unknown ibq_image_resize_mode {self.ibq_image_resize_mode!r}")

    def _cond_frames(self, data: DataDict, view_indices: tuple[int, ...]) -> list[tuple[int, int, torch.Tensor]]:
        """Conditioning frames are the current frames under the IBQ geometry, still in [0,1].

        They share ``future_grid_hw``: both frames come from the same camera under
        the same resizes, so their IBQ token grids are equal by construction.
        """
        return [
            (IBQ_KIND_COND, index, self._ibq_frame(data[f"{OBS_IMAGES}.image{index}"])) for index in view_indices
        ]

    def _model_inputs_head(
        self, *, state: torch.Tensor, processed: Any, sequence: dict[str, torch.Tensor]
    ) -> DataDict:
        """Model-input fields shared by the training and inference outputs."""
        return {
            OBS_STATE: state,
            f"{OBS_STR}.pixel_values": processed.pixel_values,
            f"{OBS_STR}.image_grid_thw": processed.image_grid_thw,
            f"{OBS_STR}.input_ids": sequence["input_ids"],
            f"{OBS_STR}.attention_mask": sequence["attention_mask"],
            FUTURE_GRID_HW_KEY: sequence["future_grid_hw"],
            FUTURE_VIEW_INDICES_KEY: sequence["future_view_indices"],
            IBQ_KIND_KEY: sequence["ibq_kind"],
            IBQ_COORDS_KEY: sequence["ibq_coords"],
            COND_CACHED_KEY: sequence["cond_cached"],
        }

    def _extra_model_inputs(self, data: DataDict) -> DataDict:
        """Policy-specific fixed-width fields appended to the common model inputs."""

        return {}
