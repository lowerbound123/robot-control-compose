# Adapted for inference-only robot-control deployment; see PROVENANCE.md.
from dataclasses import dataclass
from lerobot.configs.policies import PreTrainedConfig
from lerobot.policies.qwen3_5vla_ki.configuration_qwen3_5vla_ki import Qwen3_5VLAKIConfig
from lerobot.utils.constants import HF_HOME

@PreTrainedConfig.register_subclass("qwen3_5vla_ki_wan")
@dataclass
class Qwen3_5VLAKIWanConfig(Qwen3_5VLAKIConfig):
    # Learnable tokens for WAN conditioning
    num_learnable_tokens: int = 50

    # WAN model paths
    wan_checkpoint_path: str = f"{HF_HOME}/hub/Wan2.2-TI2V-5B"
    wan_config_path: str = f"{HF_HOME}/hub/Wan2.2-TI2V-5B"
    vae_path: str = f"{HF_HOME}/hub/Wan2.2-TI2V-5B/Wan2.2_VAE.pth"
    video_precision: str = "bfloat16"

    # WAN freeze control
    freeze_wan_dit: bool = True

    # Video settings
    num_video_frames: int = 4
    video_height: int = 224
    video_width: int = 224

    # Loss weight for video prediction
    video_loss_weight: float = 1.0

    # Two-stage training options:
    # video_loss_only: action expert only gets video loss, no action loss
    video_loss_only: bool = False
    # action_loss_only: action expert only gets action loss, learnable tokens frozen
    action_loss_only: bool = False
    # Freeze learnable tokens
    freeze_learnable_tokens: bool = False

    def __post_init__(self):
        super().__post_init__()
        if self.inference_backend == "optimized" and not self.action_loss_only:
            raise ValueError("inference_backend='optimized' requires action_loss_only=True")

    @property
    def image_delta_indices(self) -> list | None:
        n = self.num_video_frames + 1
        return [self.chunk_size * i // (n - 1) for i in range(n)]

    @property
    def action_delta_indices(self) -> list:
        return list(range(0, 2 * self.chunk_size, 2))

