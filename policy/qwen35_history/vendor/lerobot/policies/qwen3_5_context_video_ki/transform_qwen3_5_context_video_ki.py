#!/usr/bin/env python

"""Prompt construction for the KI variant: the context_video prompt plus the action chunk.

The prefix, markers, clips and FAST token sequence are exactly what
``Qwen3_5ContextVideoProcessorTransformFn`` builds.  The one addition is on the
output side: the base transform returns a fresh dict of model inputs and drops
the continuous ``action`` tensor once it has been FAST-tokenized, while the
expert regresses that tensor (already normalized, ``chunk_size`` rows, padded to
``max_action_dim`` by ``PadStateAndActionTransformFn``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import torch

from lerobot.policies.qwen3_5_context_video.tokenization_qwen3_5_context_video import ALL_MARKERS
from lerobot.policies.qwen3_5_context_video.transform_qwen3_5_context_video import (
    Qwen3_5ContextVideoProcessorTransformFn,
    _single_chw_image,
)
from lerobot.policies.qwen3_5_unified_cond.transform_qwen3_5_unified_cond import pack_ibq_frames
from lerobot.transforms.constants import DEFAULT_IMAGE_TOKEN, SYSTEM_MESSAGE
from lerobot.transforms.core import DataDict, DataTransformFn
from lerobot.utils.constants import ACTION, OBS_IMAGES, OBS_STATE

# ``M1VQADataset`` renders each ``<image>`` placeholder as this span; the prompt places the images itself.
_VQA_IMAGE_SPAN_RE = re.compile(r"<\|vision_start\|><\|image_pad\|><\|vision_end\|>")


@DataTransformFn.register_subclass("qwen3_5_context_video_ki_processor")
@dataclass
class Qwen3_5ContextVideoKIProcessorTransformFn(Qwen3_5ContextVideoProcessorTransformFn):  # noqa: N801
    def _extra_model_inputs(self, data: DataDict) -> DataDict:
        """Export the continuous chunk for the expert whenever the sample carries one.

        Inference never reads it: the model's generation prefix copies named
        observation keys only, so an evaluator that builds its prompt from a
        dataset frame may leave the target chunk in place.
        """
        if ACTION not in data:
            return {}
        return {ACTION: torch.as_tensor(data[ACTION], dtype=torch.float32)}


