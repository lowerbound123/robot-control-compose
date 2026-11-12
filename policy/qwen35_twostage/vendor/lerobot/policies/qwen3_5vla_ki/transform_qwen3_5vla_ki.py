# Adapted for inference-only robot-control deployment; see PROVENANCE.md.
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass, replace
import numpy as np
import torch
import torch.nn.functional as F
from transformers.models.qwen3_vl import Qwen3VLProcessor
from lerobot.dataset_schemas import get_schema
from lerobot.policies.qwen3_5vla_ki.action_tokens import ACTION_TOKEN_MIN, ACTION_TOKEN_MAX, ensure_qwen35_action_tokens
from lerobot.transforms.constants import SYSTEM_MESSAGE
from lerobot.transforms.core import DataDict, DataTransformFn
from lerobot.utils.constants import OBS_STATE, OBS_IMAGES, OBS_STR
LABEL_MODE_NONE = 0
LABEL_MODE_TEXT = 1
LABEL_MODE_FAST = 2
LABEL_MODE_BOTH = 3

def pad_vector(vector, new_dim):
    """Pad the last dimension of a vector to new_dim with zeros.

    Can be (sequence_length x features_dimension)
    or (features_dimension)
    """
    if vector.shape[-1] >= new_dim:
        return vector
    pad_size = new_dim - vector.shape[-1]
    return F.pad(vector, (0, pad_size))


@DataTransformFn.register_subclass("qwen3_5_ki_chat_processor")
@dataclass
class Qwen3_5KIChatProcessorTransformFn(DataTransformFn):
    """Build Qwen3.5 chat inputs with optional subtask and FAST action labels."""

    pretrained_model_name_or_path: str = "Qwen/Qwen3.5-2B"
    max_length: int = 700
    task_key: str = "task"
    language_memory_key: str = "language_memory"
    num_views: int = 3
    truncation: bool = True
    padding: str = "max_length"

    tokenize_state: bool = True
    max_state_dim: int = 32

    use_fast_action_tokens: bool = True
    action_text_key: str = "action.action_text"
    action_token_min: int = ACTION_TOKEN_MIN
    action_token_max: int = ACTION_TOKEN_MAX
    mode: str = "train"
    action_mode: str = "joint"

    def hydrate(
        self, dataset: LeRobotDataset | StreamingLeRobotDataset
    ) -> Qwen3_5KIChatProcessorTransformFn:
        schema = get_schema(dataset.meta.robot_type)
        return replace(self, action_mode=getattr(schema, "action_mode", "joint"))

    def __post_init__(self):
        self.processor = Qwen3VLProcessor.from_pretrained(self.pretrained_model_name_or_path)
        ensure_qwen35_action_tokens(self.processor.tokenizer)

    def _encode_state(self, data: DataDict) -> str:
        if not self.tokenize_state or OBS_STATE not in data:
            return ""
        state = pad_vector(deepcopy(data[OBS_STATE]), self.max_state_dim)
        state_np = state.cpu().numpy() / 3
        discretized = np.digitize(state_np, bins=np.linspace(-1, 1, 257)[:-1]) - 1
        return "State: " + " ".join(map(str, discretized))

    def __call__(self, data: DataDict) -> DataDict:
        images = []
        for i in range(self.num_views):
            image_key = f"{OBS_IMAGES}.image{i}"
            if bool(data[f"{image_key}_mask"]):
                images.append(data[image_key])

        user_text = "Task: " + str(data.get(self.task_key, ""))
        language_memory = str(data.get(self.language_memory_key, "")).strip()
        if language_memory:
            user_text += "; " + language_memory
        user_text += f"; Control Mode: <{self.action_mode}>"

        state_text = self._encode_state(data)
        if state_text:
            user_text += "; " + state_text

        has_fast = (
            self.use_fast_action_tokens
            and self.action_text_key in data
            and bool(str(data.get(self.action_text_key, "")).strip())
        )
        has_subtask = bool(str(data.get("sub_task", "")).strip())

        if has_fast and has_subtask:
            label_mode = LABEL_MODE_BOTH
        elif has_fast:
            label_mode = LABEL_MODE_FAST
        elif has_subtask:
            label_mode = LABEL_MODE_TEXT
        else:
            label_mode = LABEL_MODE_NONE

        if self.mode == "eval":
            label_mode = LABEL_MODE_NONE
            user_text += "; Output: <Subtask, Action>"
        elif label_mode == LABEL_MODE_BOTH:
            assistant_text = f"{data['sub_task']}; {data[self.action_text_key]}"
            user_text += "; Output: <SubTask, Action>"
        elif label_mode == LABEL_MODE_FAST:
            assistant_text = str(data[self.action_text_key])
            user_text += "; Output: <Action>"
        elif label_mode == LABEL_MODE_TEXT:
            assistant_text = str(data["sub_task"])
            user_text += "; Output: <SubTask>"

        if label_mode == LABEL_MODE_NONE:
            assistant_text = ""

        user_content = [{"type": "image"} for _ in images]
        user_content.append({"type": "text", "text": user_text})

        if label_mode != LABEL_MODE_NONE:
            messages_full = [
                {"role": "system", "content": SYSTEM_MESSAGE},
                {"role": "user", "content": user_content},
                {"role": "assistant", "content": [{"type": "text", "text": assistant_text}]},
            ]
            text_full = self.processor.apply_chat_template(
                messages_full,
                tokenize=False,
                add_generation_prompt=False,
            )
            inputs_full = self.processor(
                text=[text_full],
                images=images,
                do_rescale=False,
                return_tensors="pt",
                padding="max_length",
                max_length=self.max_length,
                truncation=True,
            )
            input_ids = inputs_full.input_ids[0]
            attention_mask = inputs_full.attention_mask[0]
            labels = input_ids.clone()

            if label_mode == LABEL_MODE_FAST:
                action_mask = (input_ids >= self.action_token_min) & (
                    input_ids <= self.action_token_max
                )
                action_positions = action_mask.nonzero(as_tuple=False)
                if action_positions.numel() > 0:
                    labels[: action_positions[0].item()] = -100
                else:
                    labels[:] = -100
            else:
                messages_prompt = [
                    {"role": "system", "content": SYSTEM_MESSAGE},
                    {"role": "user", "content": user_content},
                ]
                text_prompt = self.processor.apply_chat_template(
                    messages_prompt,
                    tokenize=False,
                    add_generation_prompt=True,
                )
                inputs_prompt = self.processor(
                    text=[text_prompt],
                    images=images,
                    do_rescale=False,
                    return_tensors="pt",
                    padding=False,
                )
                labels[: inputs_prompt.input_ids.shape[1]] = -100

            labels[attention_mask == 0] = -100
        else:
            messages = [
                {"role": "system", "content": SYSTEM_MESSAGE},
                {"role": "user", "content": user_content},
            ]
            is_training = self.mode == "train"
            text_prompt = self.processor.apply_chat_template(
                messages,
                tokenize=False,
                add_generation_prompt=not is_training,
            )
            processor_kwargs = {
                "text": [text_prompt],
                "images": images,
                "do_rescale": False,
                "return_tensors": "pt",
                "padding": self.padding if is_training else False,
            }
            if is_training:
                processor_kwargs.update(
                    max_length=self.max_length,
                    truncation=self.truncation,
                )
            inputs_full = self.processor(**processor_kwargs)
            input_ids = inputs_full.input_ids[0]
            attention_mask = inputs_full.attention_mask[0]
            labels = torch.full_like(input_ids, -100)

        fast_token_mask = torch.zeros_like(input_ids, dtype=torch.bool)
        if label_mode in (LABEL_MODE_FAST, LABEL_MODE_BOTH):
            action_positions = (
                (input_ids >= self.action_token_min) & (input_ids <= self.action_token_max)
            ).nonzero(as_tuple=False)
            if action_positions.numel() > 0:
                first_action_index = action_positions[0].item()
                fast_token_mask[first_action_index:] = attention_mask[first_action_index:].bool()

        data[f"{OBS_STR}.input_ids"] = input_ids
        data[f"{OBS_STR}.attention_mask"] = attention_mask
        data[f"{OBS_STR}.pixel_values"] = inputs_full.pixel_values
        data[f"{OBS_STR}.image_grid_thw"] = inputs_full.image_grid_thw
        data[f"{OBS_STR}.fast_token_mask"] = fast_token_mask
        data["VQA.labels"] = labels
        data["vqa_type"] = torch.tensor(
            0 if label_mode == LABEL_MODE_NONE else 2,
            dtype=torch.long,
        )
        data["label_mode"] = torch.tensor(label_mode, dtype=torch.long)
        return data

