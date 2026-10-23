"""Local Qwen3.5 KI/Wan inference, adapted from deploydev at 617449f.

Heavy dependencies are loaded only inside lifecycle/inference calls. All calls
are serialized on the policy runtime's inference executor.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .statistics import read_action_stats

OBS_STR = "observation"


class Qwen35Backend:
    def __init__(self, config: dict):
        self.config = dict(config["model"])
        self.checkpoint = Path(self.config["checkpoint"]).expanduser().resolve()
        if self.config.get("workflow", "single") not in {"single", "two_stage"}:
            raise ValueError("workflow must be single or two_stage")
        self.policy = self.policy_config = self.input_transforms = self.unnormalize = (
            None
        )
        self.debug_tokenizer = None
        self._chunk_size = 50
        self._closed = False
        self.reset_context()

    @property
    def chunk_size(self) -> int:
        return self._chunk_size

    def torch_dtype(self):
        import torch

        return {
            "float32": torch.float32,
            "bfloat16": torch.bfloat16,
            "float16": torch.float16,
        }[self.config.get("dtype", "bfloat16")]

    def load(self) -> None:
        import torch
        from lerobot.configs.policies import PreTrainedConfig
        from lerobot.dataset_schemas import get_schema
        from lerobot.policies.qwen3_5vla_ki.transform_qwen3_5vla_ki import (
            Qwen3_5KIChatProcessorTransformFn,
        )
        from lerobot.policies.qwen3_5vla_ki_wan import (
            Qwen3_5VLAKIWanConfig,
            Qwen3_5VLAKIWanPolicy,
        )
        from lerobot.transforms.core import (
            NormalizeTransformFn,
            RemapImageKeyTransformFn,
            ResizeImagesWithPadFn,
            UnNormalizeTransformFn,
            compose,
        )

        if self._closed:
            raise RuntimeError("backend is closed")
        if not torch.cuda.is_available():
            raise RuntimeError("Qwen3.5 policy requires a CUDA GPU")
        if not self.checkpoint.is_dir():
            raise FileNotFoundError(
                f"checkpoint directory does not exist: {self.checkpoint}"
            )
        torch.backends.cudnn.benchmark = True
        torch.backends.cuda.matmul.allow_tf32 = True
        policy_config = PreTrainedConfig.from_pretrained(
            self.checkpoint, local_files_only=True
        )
        if not isinstance(policy_config, Qwen3_5VLAKIWanConfig):
            raise TypeError(
                f"checkpoint is not Qwen3_5VLAKIWanConfig: {type(policy_config).__name__}"
            )
        policy_config.compile_model = bool(self.config.get("compile_model", False))
        policy_config.compile_mode = str(
            self.config.get("compile_mode", "reduce-overhead")
        )
        policy_config.inference_backend = str(
            self.config.get("inference_backend", "optimized")
        )
        policy_config.action_loss_only = bool(self.config.get("action_loss_only", True))
        if (
            policy_config.inference_backend != "optimized"
            or not policy_config.action_loss_only
        ):
            raise ValueError("only optimized action_loss_only inference is supported")
        self._chunk_size = int(policy_config.chunk_size)
        if self._chunk_size != 50:
            raise ValueError(
                f"checkpoint chunk_size must be 50, got {self._chunk_size}"
            )
        self.policy_config = policy_config
        self.policy = Qwen3_5VLAKIWanPolicy.from_pretrained(
            config=policy_config,
            pretrained_name_or_path=self.checkpoint,
            local_files_only=True,
        )
        self.policy.cuda().to(self.torch_dtype()).eval()
        self.policy.requires_grad_(False)
        if self.config.get("workflow") == "two_stage":
            from transformers import AutoTokenizer

            self.debug_tokenizer = AutoTokenizer.from_pretrained(
                policy_config.vlm_model_name_or_path
            )
        state_stats, action_stats = self.read_stats()
        self.unnormalize = UnNormalizeTransformFn(
            selected_keys=["action"], norm_stats=action_stats
        )
        schema = get_schema(self.config.get("schema_key", "arx_acone"))
        self.input_transforms = compose(
            [
                ResizeImagesWithPadFn(
                    height=int(self.config.get("image_height", 224)),
                    width=int(self.config.get("image_width", 224)),
                    mapping=schema.image_mapping,
                ),
                RemapImageKeyTransformFn(mapping=schema.image_mapping),
                NormalizeTransformFn(
                    selected_keys=["observation.state"], norm_stats=state_stats
                ),
                Qwen3_5KIChatProcessorTransformFn(mode="eval"),
            ]
        )

    def read_stats(self) -> tuple[dict, dict]:
        return read_action_stats(self.checkpoint, self.config.get("stats_key", "arx_acone"))

    def warmup(self) -> None:
        import torch

        if self.policy is None:
            raise RuntimeError("backend is not loaded")
        inputs = self.make_benchmark_inputs()
        with torch.no_grad():
            for _ in range(int(self.config.get("warmup_runs", 2))):
                self.policy.predict_action_chunk(inputs)
        torch.cuda.synchronize()

    def make_benchmark_inputs(self) -> dict[str, Any]:
        import torch

        dtype = self.torch_dtype()
        inputs = {}
        for i in range(3):
            inputs[f"observation.images.image{i}"] = torch.rand(
                (1, 3, 224, 224), dtype=dtype
            ).cuda()
            inputs[f"observation.images.image{i}_mask"] = torch.tensor([True]).cuda()
        inputs.update(
            {
                "observation.pixel_values": torch.rand(
                    (1, 768, 1536), dtype=dtype
                ).cuda(),
                "observation.image_grid_thw": torch.tensor([[[1, 16, 16]] * 3]).cuda(),
                "observation.input_ids": torch.tensor(
                    [[248056] * 192 + [777] * 54]
                ).cuda(),
                "observation.attention_mask": torch.tensor([[1] * 246]).cuda(),
                "observation.state": torch.rand((1, 16), dtype=dtype).cuda(),
                "action": torch.rand((1, 50, 16), dtype=dtype).cuda(),
                "task": ["dummy sample"],
            }
        )
        return inputs

    def predict(
        self, qpos: np.ndarray, images: dict[str, np.ndarray], instruction: str
    ) -> np.ndarray:
        import torch

        if self.policy is None or self._closed:
            raise RuntimeError("backend is not loaded")
        with torch.no_grad():
            inputs = self._prepare_inputs(qpos, images, instruction)
            if self.config.get("workflow") == "two_stage":
                key_ids = f"{OBS_STR}.input_ids"
                if self._subtask_tokens is None or self._subtask_uses_left <= 0:
                    _ensure_no_fast_tokens(
                        inputs,
                        self.policy_config.action_token_min,
                        self.policy_config.action_token_max,
                    )
                    prompt_length = inputs[key_ids].shape[1]
                    _, self._subtask = _append_subtask_until_first_fast_token(
                        self.policy,
                        inputs,
                        action_token_min=self.policy_config.action_token_min,
                        action_token_max=self.policy_config.action_token_max,
                        max_new_tokens=int(
                            self.config.get("subtask_max_new_tokens", 128)
                        ),
                        debug_tokenizer=self.debug_tokenizer,
                    )
                    self._subtask_tokens = inputs[key_ids][:, prompt_length:].clone()
                    self._subtask_uses_left = int(
                        self.config.get("subtask_refresh_chunks", 3)
                    )
                else:
                    _append_cached_subtask_tokens(inputs, self._subtask_tokens)
                prediction = self.policy.predict_action_chunk(inputs)
                self._subtask_uses_left -= 1
            else:
                prediction = self.policy.predict_action_chunk(inputs)
            return self.finalize_actions(prediction, qpos)

    def state_with_offsets(self, qpos: np.ndarray) -> np.ndarray:
        state = np.asarray(qpos, dtype=np.float32).copy()
        if state.shape != (14,) or not np.isfinite(state).all():
            raise ValueError("qpos must be finite with shape (14,)")
        for index, offset in self.config.get("state_offsets", {}).items():
            state[int(index)] += float(offset)
        return state

    def _prepare_inputs(self, qpos, images, instruction) -> dict:
        import torch

        sample = {
            "observation.state": torch.as_tensor(self.state_with_offsets(qpos))
            .contiguous()
            .cuda(),
            "task": instruction,
        }
        for camera, name in [
            ("head", "head"),
            ("left_wrist", "hand_left"),
            ("right_wrist", "hand_right"),
        ]:
            image = np.asarray(images[camera])
            if image.ndim != 3 or image.shape[2] != 3 or image.dtype != np.uint8:
                raise ValueError(f"{camera} must be RGB uint8 HWC")
            sample[f"images.rgb.{name}"] = (
                torch.as_tensor(image.copy()).contiguous().cuda().to(self.torch_dtype())
                / 255.0
            ).permute(2, 0, 1)
        return self.batch_sample(self.input_transforms(sample))

    def batch_sample(self, sample) -> dict:
        import torch

        return {
            key: [value]
            if key == "task"
            else value[None].cuda()
            if value.dtype in (torch.int64, torch.bool)
            else value[None].cuda().to(dtype=self.torch_dtype())
            for key, value in sample.items()
        }

    def finalize_actions(self, prediction, qpos) -> np.ndarray:
        import torch

        if (
            prediction.ndim != 3
            or prediction.shape[0] != 1
            or prediction.shape[1] != self.chunk_size
            or prediction.shape[2] < 16
        ):
            raise ValueError("prediction shape must be [1,50,>=16]")
        prediction = prediction[0, :, :16]
        prediction = torch.cat(
            [
                prediction[:, :6],
                prediction[:, 7:8],
                prediction[:, 8:14],
                prediction[:, 15:16],
            ],
            dim=1,
        )
        prediction = self.unnormalize({"action": prediction})["action"]
        if self.config.get("action_mode", "delta") == "delta":
            # Clone: torch.as_tensor may alias the read-only observation on CPU.
            initial = torch.as_tensor(
                np.asarray(qpos)[None], device=prediction.device, dtype=prediction.dtype
            ).clone()
            initial[:, [6, 13]] = 0
            prediction = prediction + initial
        actions = prediction.to(torch.float32).detach().cpu().numpy()
        if actions.shape != (50, 14) or not np.isfinite(actions).all():
            raise ValueError(
                "actions must have shape [50,14] and contain only finite values"
            )
        return actions

    def reset_context(self) -> None:
        self._subtask_tokens = None
        self._subtask_uses_left = 0
        self._subtask = ""

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.reset_context()
        self.debug_tokenizer = self.input_transforms = self.unnormalize = (
            self.policy_config
        ) = self.policy = None
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def _ensure_no_fast_tokens(
    inputs: dict, action_token_min: int, action_token_max: int
) -> None:

    key_ids = f"{OBS_STR}.input_ids"
    key_attention = f"{OBS_STR}.attention_mask"
    key_fast_mask = f"{OBS_STR}.fast_token_mask"
    input_ids = inputs[key_ids]
    fast_positions = (input_ids >= action_token_min) & (input_ids <= action_token_max)
    if not fast_positions.any():
        inputs.pop(key_fast_mask, None)
        return
    if input_ids.shape[0] != 1:
        raise ValueError("FAST token stripping requires batch size 1")
    keep = ~fast_positions
    inputs[key_ids] = input_ids[keep].unsqueeze(0)
    inputs[key_attention] = inputs[key_attention][keep].unsqueeze(0)
    inputs.pop(key_fast_mask, None)


def _pack_vision_inputs(pixel_values: Any, image_grid_thw: Any) -> tuple[Any, Any]:
    image_grid_thw = image_grid_thw.reshape(-1, 3)
    if pixel_values.ndim > 2:
        pixel_values = pixel_values.reshape(-1, pixel_values.shape[-1])
    return pixel_values, image_grid_thw


def _append_subtask_until_first_fast_token(
    policy: Any,
    inputs: dict,
    *,
    action_token_min: int,
    action_token_max: int,
    max_new_tokens: int,
    debug_tokenizer: Any,
) -> tuple[int, str]:
    import torch
    from transformers import StoppingCriteria, StoppingCriteriaList

    class StopOnFastToken(StoppingCriteria):
        def __call__(self, input_ids, scores, **kwargs) -> bool:
            del scores, kwargs
            last_tokens = input_ids[:, -1]
            is_fast = (last_tokens >= action_token_min) & (
                last_tokens <= action_token_max
            )
            return bool(is_fast.all().item())

    key_ids = f"{OBS_STR}.input_ids"
    key_attention = f"{OBS_STR}.attention_mask"
    key_fast_mask = f"{OBS_STR}.fast_token_mask"
    key_pixels = f"{OBS_STR}.pixel_values"
    key_grid = f"{OBS_STR}.image_grid_thw"
    _ensure_no_fast_tokens(inputs, action_token_min, action_token_max)
    language_tokens = inputs[key_ids]
    language_masks = inputs[key_attention]
    prompt_length = language_tokens.shape[1]
    pixel_values, image_grid_thw = _pack_vision_inputs(
        inputs[key_pixels], inputs[key_grid]
    )
    generated = policy.model.qwen3_5_with_expert.qwen3_5.generate(
        input_ids=language_tokens,
        attention_mask=language_masks,
        pixel_values=pixel_values,
        image_grid_thw=image_grid_thw,
        do_sample=False,
        max_new_tokens=max_new_tokens,
        stopping_criteria=StoppingCriteriaList([StopOnFastToken()]),
    )
    new_tokens = generated[:, prompt_length:]
    if new_tokens.numel() == 0:
        inputs[key_fast_mask] = torch.zeros_like(inputs[key_ids], dtype=torch.bool)
        return 0, ""
    fast_positions = (new_tokens >= action_token_min) & (new_tokens <= action_token_max)
    if fast_positions.any():
        if new_tokens.shape[0] != 1:
            raise ValueError("FAST token trimming requires batch size 1")
        first_fast = fast_positions[0].nonzero(as_tuple=False)[0].item()
        subtask_tokens = new_tokens[:, :first_fast]
    else:
        subtask_tokens = new_tokens
    subtask_text = debug_tokenizer.decode(
        subtask_tokens[0].detach().cpu().tolist(), skip_special_tokens=True
    ).strip()
    print(f"  generated subtask: {subtask_text!r}")
    if subtask_tokens.numel() > 0:
        masks = torch.ones(
            subtask_tokens.shape,
            dtype=language_masks.dtype,
            device=language_masks.device,
        )
        inputs[key_ids] = torch.cat([language_tokens, subtask_tokens], dim=1)
        inputs[key_attention] = torch.cat([language_masks, masks], dim=1)
    _ensure_no_fast_tokens(inputs, action_token_min, action_token_max)
    inputs[key_fast_mask] = torch.zeros_like(inputs[key_ids], dtype=torch.bool)
    return subtask_tokens.shape[1], subtask_text


def _append_cached_subtask_tokens(inputs: dict, cached_tokens: Any) -> None:
    import torch

    key_ids = f"{OBS_STR}.input_ids"
    key_attention = f"{OBS_STR}.attention_mask"
    key_fast_mask = f"{OBS_STR}.fast_token_mask"
    cached_tokens = cached_tokens.to(device=inputs[key_ids].device)
    masks = torch.ones(
        cached_tokens.shape,
        dtype=inputs[key_attention].dtype,
        device=inputs[key_attention].device,
    )
    inputs[key_ids] = torch.cat([inputs[key_ids], cached_tokens], dim=1)
    inputs[key_attention] = torch.cat([inputs[key_attention], masks], dim=1)
    inputs[key_fast_mask] = torch.zeros_like(inputs[key_ids], dtype=torch.bool)
