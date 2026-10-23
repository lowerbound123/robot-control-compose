# Adapted for inference-only robot-control deployment; see PROVENANCE.md.
from collections import deque
import torch
from torch import Tensor
from lerobot.policies.pretrained import PreTrainedPolicy
from lerobot.policies.qwen3_5vla_ki.modeling_qwen3_5vla_ki import pad_vector
from lerobot.policies.qwen3_5vla_ki_wan.configuration_qwen3_5vla_ki_wan import Qwen3_5VLAKIWanConfig
from lerobot.utils.constants import ACTION, OBS_PREFIX, OBS_STATE
from lerobot.utils.utils import format_big_number

class Qwen3_5VLAKIWanPolicy(PreTrainedPolicy):
    """Qwen3.5 VLA-KI policy with WAN video auxiliary supervision."""

    config_class = Qwen3_5VLAKIWanConfig
    name = "qwen3_5vla_ki_wan"
    _checkpoint_excluded_prefixes = ("model.wan_video_model.",)

    def __init__(self, config: Qwen3_5VLAKIWanConfig):
        super().__init__(config)
        config.validate_features()
        self.config = config

        if config.inference_backend == "optimized":
            from lerobot.policies.qwen3_5vla_ki_wan.modeling_qwen3_5vla_ki_wan_opt import (
                Qwen3_5VLAKIWanFast,
            )

            self.model = Qwen3_5VLAKIWanFast(config)
        else:
            raise ValueError("Only optimized inference is included in this runtime")

        if config.gradient_checkpointing:
            self.model.gradient_checkpointing_enable()

        self.model.to(config.device)
        self.reset()

    def __str__(self) -> str:
        lines = ["=" * 60, f"Policy: {self.__class__.__name__}", ""]
        num_total = sum(p.numel() for p in self.parameters())
        num_trainable = sum(p.numel() for p in self.parameters() if p.requires_grad)
        num_vlm = sum(p.numel() for p in self.model.qwen3_5_with_expert.qwen3_5.parameters())
        num_expert = sum(p.numel() for p in self.model.qwen3_5_with_expert.action_expert.parameters())
        num_wan = (
            sum(p.numel() for p in self.model.wan_video_model.parameters())
            if hasattr(self.model, "wan_video_model")
            else 0
        )
        lines.append("Parameter statistics:")
        lines.append(f"  - Total params        : {format_big_number(num_total)}")
        lines.append(f"  - Trainable params    : {format_big_number(num_trainable)}")
        lines.append(f"  - Qwen3_5 params      : {format_big_number(num_vlm)}")
        lines.append(f"  - Action expert params: {format_big_number(num_expert)}")
        lines.append(f"  - WAN params          : {format_big_number(num_wan)}")
        lines.append(f"  - Learnable tokens    : {self.config.num_learnable_tokens}")
        lines.append(f"  - Knowledge insulation: {self.config.knowledge_insulation}")
        lines.append(f"  - Inference backend   : {self.config.inference_backend}")
        lines.append(f"  - Freeze WAN DiT      : {self.config.freeze_wan_dit}")
        lines.append("=" * 60)
        return "\n".join(lines)

    def to(self, *args, **kwargs):
        super().to(*args, **kwargs)
        self.model.action_out_proj.to(torch.float32)
        if hasattr(self.model, "_cast_action_expert_to_fp32"):
            self.model._cast_action_expert_to_fp32()
        return self

    def state_dict(self, *args, **kwargs):
        state = super().state_dict(*args, **kwargs)
        for key in list(state.keys()):
            if key.startswith(self._checkpoint_excluded_prefixes):
                del state[key]

        metadata = getattr(state, "_metadata", None)
        if metadata is not None:
            for key in list(metadata.keys()):
                if key == "model.wan_video_model" or key.startswith("model.wan_video_model."):
                    del metadata[key]
        return state

    def get_optim_params(self) -> dict:
        return self.parameters()

    def reset(self):
        self._action_queue = deque(maxlen=self.config.n_action_steps)
        self._queues = {ACTION: deque(maxlen=self.config.n_action_steps)}

    def prepare_state(self, batch):
        return pad_vector(batch[OBS_STATE], self.config.max_state_dim)

    def prepare_action(self, batch):
        return pad_vector(batch[ACTION], self.config.max_action_dim)

    @torch.no_grad()
    def select_action(self, batch: dict[str, Tensor]) -> Tensor:
        self.eval()
        if len(self._action_queue) == 0:
            actions = self.predict_action_chunk(batch)[:, :self.config.n_action_steps]
            self._action_queue.extend(actions.transpose(0, 1))
        return self._action_queue.popleft()

    @torch.no_grad()
    def predict_action_chunk(self, batch: dict[str, Tensor]) -> Tensor:
        self.eval()
        pixel_values = batch[f"{OBS_PREFIX}pixel_values"]
        image_grid_thw = batch[f"{OBS_PREFIX}image_grid_thw"]
        lang_tokens = batch[f"{OBS_PREFIX}input_ids"]
        lang_masks = batch[f"{OBS_PREFIX}attention_mask"]
        fast_token_mask = batch.get(f"{OBS_PREFIX}fast_token_mask")
        state = self.prepare_state(batch)

        actions = self.model.sample_actions(
            pixel_values, image_grid_thw, lang_tokens, lang_masks, state,
            fast_token_mask=fast_token_mask,
        )

        original_action_dim = self.config.output_features[ACTION].shape[0]
        return actions[:, :, :original_action_dim]


    def forward(self, batch: dict[str, Tensor]) -> tuple[Tensor, dict]:
        raise NotImplementedError("Training is not included in this inference runtime")

