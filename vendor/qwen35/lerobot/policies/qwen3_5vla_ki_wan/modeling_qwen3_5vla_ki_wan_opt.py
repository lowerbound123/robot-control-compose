"""Accelerated Qwen3.5 VLA-KI-Wan inference for real-robot online deployment.

This is a drop-in replacement of `modeling_qwen3_5vla_ki_wan.py` for inference.
It strips out the WAN video model entirely (VAE + DiT + flow scheduler) since
the action prediction path never needs them, and reuses the SDPA + CUDA Graph
optimizations from `modeling_qwen3_5vla_ki_opt.py`.

Differences vs. the training-time `Qwen3_5VLAKIWan`:
  * No `wan_video_model`, `learnable_to_wan_proj`, `fm_video_scheduler`,
    `_wan_grid_sizes` — saves several GB of weights/VRAM and avoids loading
    Wan2.2 checkpoints at inference time.
  * No `denoise_step_full`, `wan_dit_forward`, `generate_video`, or
    `predict_action_chunk_with_video`.
  * The suffix layout (state + learnable_tokens + action+time) is preserved so
    action denoising is numerically equivalent to the training-time module.
  * CUDA-Graph-only fast path; no debug `_no_graph` variant.

The public `Qwen3_5VLAKIWanPolicy` selects this backend through
`config.inference_backend="optimized"`. Since checkpoint loading defaults to
`strict=False`, WAN-only checkpoint keys are ignored by this inference backend.
"""

import logging

import torch
import torch.nn.functional as F
from torch import Tensor, nn

from lerobot.policies.qwen3_5vla_ki.modeling_qwen3_5vla_ki import (
    create_sinusoidal_pos_embedding,
    make_att_2d_masks,
)
from lerobot.policies.qwen3_5vla_ki.modeling_qwen3_5vla_ki_opt import Qwen3_5VLAKIFast
from lerobot.policies.qwen3_5vla_ki_wan.configuration_qwen3_5vla_ki_wan import (
    Qwen3_5VLAKIWanConfig,
)
from lerobot.utils.constants import ACTION, OBS_PREFIX, OBS_STATE

logger = logging.getLogger(__name__)


class Qwen3_5VLAKIWanFast(Qwen3_5VLAKIFast):
    """Accelerated Qwen3.5 VLA-KI-Wan: SDPA + CUDA Graph, no WAN video branch."""

    def __init__(self, config: Qwen3_5VLAKIWanConfig):
        if not config.action_loss_only:
            raise ValueError(
                "Qwen3_5VLAKIWanFast requires action_loss_only=True so the WAN branch is not loaded"
            )
        # Goes Qwen3_5VLAKIFast -> Qwen3_5VLAKI; never touches Qwen3_5VLAKIWan,
        # so wan_video_model / learnable_to_wan_proj / fm_video_scheduler are
        # never instantiated.
        super().__init__(config)

        action_expert_hidden_size = self.qwen3_5_with_expert.action_expert.config.hidden_size

        # Learnable tokens — these ARE part of the suffix during action
        # denoising, so they must be present (their values come from the
        # checkpoint via load_state_dict).
        self.learnable_tokens = nn.Parameter(
            torch.zeros(config.num_learnable_tokens, action_expert_hidden_size)
        )
        nn.init.trunc_normal_(self.learnable_tokens, std=0.02)
        self.learnable_tokens_in_proj = nn.Linear(
            action_expert_hidden_size, action_expert_hidden_size
        )

        # Inference-only: freeze everything we own.
        self.learnable_tokens.requires_grad = False
        for p in self.learnable_tokens_in_proj.parameters():
            p.requires_grad = False

    def _cast_action_expert_to_fp32(self):
        """Extend parent to also cast wan-specific learnable token modules."""
        super()._cast_action_expert_to_fp32()
        self.learnable_tokens.data = self.learnable_tokens.data.to(torch.float32)
        self.learnable_tokens_in_proj.to(torch.float32)

    # ------------------------------------------------------------------
    # Suffix layout helpers
    # ------------------------------------------------------------------

    def _full_suffix_len(self) -> int:
        """Total suffix length: [state?] + learnable + action+time."""
        n_state = 0 if self.config.tokenize_state else 1
        return n_state + self.config.num_learnable_tokens + self.config.chunk_size

    def _build_suffix_att_mask_list(self) -> list[int]:
        """Block-diagonal-ish att mask values for [state, learnable, action+time]."""
        n_lt = self.config.num_learnable_tokens
        chunk = self.config.chunk_size
        values: list[int] = []
        if not self.config.tokenize_state:
            values += [1]
        values += [1] + [0] * (n_lt - 1)
        values += [1] + [0] * (chunk - 1)
        return values

    # ------------------------------------------------------------------
    # CUDA-Graph-friendly suffix masks/embeddings
    # ------------------------------------------------------------------

    def _get_static_suffix_masks(self, bsize: int, device: torch.device, dtype: torch.dtype):
        """Override parent to include learnable token slots in the static mask."""
        key = (bsize, device)
        if not hasattr(self, "_suffix_masks_cache"):
            self._suffix_masks_cache = {}
        if key not in self._suffix_masks_cache:
            att_masks_list = self._build_suffix_att_mask_list()
            att_masks = torch.tensor(att_masks_list, dtype=dtype, device=device)
            suffix_len = len(att_masks_list)
            pad_masks = torch.ones(bsize, suffix_len, dtype=torch.bool, device=device)
            self._suffix_masks_cache[key] = (pad_masks, att_masks.unsqueeze(0).expand(bsize, -1))
        return self._suffix_masks_cache[key]

    def embed_suffix_fast(self, state, noisy_actions, timestep):
        """CUDA-Graph-compatible suffix embedding.

        Layout matches Qwen3_5VLAKIWan.embed_suffix:
            [state(1)?] [learnable(N)] [action+time(chunk_size)]
        """
        embs: list[Tensor] = []
        bsize = state.shape[0]
        device = state.device

        # State token
        if not self.config.tokenize_state:
            if self.state_proj.weight.dtype == torch.float32:
                state = state.to(torch.float32)
            state_emb = self.state_proj(state)
            embs.append(state_emb[:, None, :])

        # Learnable tokens (project then broadcast over batch)
        lt_emb = self.learnable_tokens_in_proj(self.learnable_tokens)
        lt_emb = lt_emb[None].expand(bsize, -1, -1)
        embs.append(lt_emb)

        # Action + time tokens
        time_emb = create_sinusoidal_pos_embedding(
            timestep,
            self.action_in_proj.out_features,
            min_period=self.config.min_period,
            max_period=self.config.max_period,
            device=device,
        ).type(dtype=timestep.dtype)

        action_emb = self.action_in_proj(noisy_actions)
        time_emb = time_emb[:, None, :].expand_as(action_emb)
        action_time_emb = torch.cat([action_emb, time_emb], dim=2)

        x = self.action_time_mlp_in(action_time_emb)
        x = F.silu(x)
        action_time_emb = self.action_time_mlp_out(x)

        embs.append(action_time_emb)
        embs = torch.cat(embs, dim=1)

        pad_masks, att_masks = self._get_static_suffix_masks(bsize, device, embs.dtype)
        return embs, pad_masks, att_masks

    # ------------------------------------------------------------------
    # Static mask + position IDs (full suffix length, not just chunk_size)
    # ------------------------------------------------------------------

    def _build_static_mask_and_pos(
        self,
        state: Tensor,
        prefix_pad_masks: Tensor,
        max_prefix_position_ids: Tensor,
        fast_mask: Tensor | None,
        dtype: torch.dtype,
    ) -> tuple[Tensor, Tensor]:
        """Pre-compute attention mask + position IDs over the FULL suffix."""
        suffix_len = self._full_suffix_len()
        batch_size = prefix_pad_masks.shape[0]
        prefix_len = prefix_pad_masks.shape[1]
        device = prefix_pad_masks.device

        suffix_pad_masks = torch.ones(batch_size, suffix_len, dtype=torch.bool, device=device)
        att_mask_values = self._build_suffix_att_mask_list()
        suffix_att_masks = torch.tensor(
            att_mask_values, dtype=dtype, device=device
        )[None].expand(batch_size, suffix_len)

        prefix_pad_2d_masks = prefix_pad_masks[:, None, :].expand(
            batch_size, suffix_len, prefix_len
        )
        suffix_att_2d_masks = make_att_2d_masks(suffix_pad_masks, suffix_att_masks)
        full_att_2d_masks = torch.cat([prefix_pad_2d_masks, suffix_att_2d_masks], dim=2)

        if fast_mask is not None:
            mask_b = fast_mask.to(device=device, dtype=torch.bool)
            full_att_2d_masks[:, :, :prefix_len] &= ~mask_b[:, None, :]

        attention_mask_4d = self._prepare_attention_masks_4d(full_att_2d_masks)

        position_ids = (
            torch.arange(1, suffix_len + 1, device=device)
            .repeat(3, 1, 1)
            .to(max_prefix_position_ids)
            + max_prefix_position_ids
        )
        return attention_mask_4d, position_ids

    # ------------------------------------------------------------------
    # CUDA Graph capture — only difference vs. parent: static_x_t length
    # is chunk_size (the noisy-actions length), not the full suffix length.
    # ------------------------------------------------------------------

    def _capture_graph(
        self,
        prefix_len: int,
        state: Tensor,
        prefix_pad_masks: Tensor,
        prefix_kv_list: list[tuple[Tensor, Tensor]],
        max_prefix_position_ids: Tensor,
        fast_mask: Tensor | None,
        dtype: torch.dtype,
    ):
        device = state.device
        batch_size = state.shape[0]
        chunk = self.config.chunk_size

        attention_mask_4d, position_ids = self._build_static_mask_and_pos(
            state, prefix_pad_masks, max_prefix_position_ids, fast_mask, dtype
        )

        # x_t / timestep are the only inputs that change per denoise step.
        static_x_t = torch.zeros(
            batch_size, chunk, self.config.max_action_dim, device=device, dtype=dtype
        )
        static_timestep = torch.ones(batch_size, device=device, dtype=dtype)

        static_kv_list = [
            (k.to(dtype).clone(), v.to(dtype).clone()) for k, v in prefix_kv_list
        ]

        buffers = {
            "x_t": static_x_t,
            "timestep": static_timestep,
            "state": state.clone(),
            "prefix_pad_masks": prefix_pad_masks.clone(),
            "max_prefix_position_ids": max_prefix_position_ids.clone(),
            "attention_mask_4d": attention_mask_4d,
            "position_ids": position_ids,
            "prefix_kv_list": static_kv_list,
        }

        torch.cuda.synchronize()
        for _ in range(2):
            _ = self._denoise_step_fast(
                buffers["state"],
                buffers["prefix_pad_masks"],
                buffers["prefix_kv_list"],
                buffers["max_prefix_position_ids"],
                buffers["x_t"],
                buffers["timestep"],
                attention_mask_4d=buffers["attention_mask_4d"],
                position_ids=buffers["position_ids"],
            )
        torch.cuda.synchronize()

        graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(graph):
            static_output = self._denoise_step_fast(
                buffers["state"],
                buffers["prefix_pad_masks"],
                buffers["prefix_kv_list"],
                buffers["max_prefix_position_ids"],
                buffers["x_t"],
                buffers["timestep"],
                attention_mask_4d=buffers["attention_mask_4d"],
                position_ids=buffers["position_ids"],
            )

        buffers["output"] = static_output
        self._graphs[prefix_len] = graph
        self._static_buffers[prefix_len] = buffers
        logger.info(f"CUDA Graph captured for prefix_len={prefix_len}")


if __name__ == "__main__":
    """Smoke test: build the fast policy from a wan config (no real WAN ckpt
    needed since we never instantiate the WAN model), run a dummy
    predict_action_chunk to exercise CUDA-Graph capture + replay.
    """
    import time

    from lerobot.policies.qwen3_5vla_ki_wan.modeling_qwen3_5vla_ki_wan import (
        Qwen3_5VLAKIWanPolicy,
    )

    torch.manual_seed(0)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    cfg = Qwen3_5VLAKIWanConfig(
        vlm_model_name_or_path="Qwen/Qwen3.5-2B",
        action_expert_hidden_size=1024,
        action_expert_intermediate_size=3072,
        knowledge_insulation=True,
        block_action_attend_fast_tokens=True,
        num_learnable_tokens=8,
        inference_backend="optimized",
        action_loss_only=True,
        # WAN model paths irrelevant — they are never read.
    )
    dtype = torch.float32 if cfg.dtype == "float32" else torch.bfloat16

    policy = Qwen3_5VLAKIWanPolicy(cfg)
    policy.eval()

    patch_embed = policy.model.qwen3_5_with_expert.qwen3_5.visual.patch_embed
    patch_size = patch_embed.patch_size
    temporal_patch = patch_embed.temporal_patch_size
    in_channels = patch_embed.in_channels
    patch_feat_dim = in_channels * temporal_patch * patch_size * patch_size
    image_token_id = policy.model.qwen3_5_with_expert.qwen3_5.config.image_token_id

    B, L = 1, 48
    grid_t, grid_h, grid_w = 1, 4, 4
    patches_per_image = grid_t * grid_h * grid_w
    spatial_merge_size = policy.model.qwen3_5_with_expert.qwen3_5.visual.config.spatial_merge_size
    tokens_per_image = patches_per_image // (spatial_merge_size ** 2)

    pixel_values = torch.randn(B, patches_per_image, patch_feat_dim, device=device, dtype=dtype)
    image_grid_thw = torch.tensor([[grid_t, grid_h, grid_w]] * B, device=device, dtype=torch.long)
    input_ids = torch.randint(1000, 2000, (B, L), device=device, dtype=torch.long)
    input_ids[:, 10 : 10 + tokens_per_image] = image_token_id
    attention_mask = torch.ones(B, L, device=device, dtype=torch.long)

    batch = {
        f"{OBS_PREFIX}pixel_values": pixel_values,
        f"{OBS_PREFIX}image_grid_thw": image_grid_thw,
        f"{OBS_PREFIX}input_ids": input_ids,
        f"{OBS_PREFIX}attention_mask": attention_mask,
        OBS_STATE: torch.zeros(B, cfg.max_state_dim, device=device, dtype=dtype),
        ACTION: torch.zeros(B, cfg.chunk_size, 14, device=device, dtype=dtype),
    }

    actions = policy.predict_action_chunk(batch)
    expected_action_dim = cfg.output_features[ACTION].shape[0]
    assert actions.shape == (B, cfg.chunk_size, expected_action_dim), (
        f"unexpected action shape {tuple(actions.shape)} (expected ({B}, {cfg.chunk_size}, {expected_action_dim}))"
    )
    print(f"action shape: {tuple(actions.shape)}")

    if device.type == "cuda":
        for _ in range(3):
            policy.predict_action_chunk(batch)
        torch.cuda.synchronize()
        t0 = time.time()
        N = 20
        for _ in range(N):
            policy.predict_action_chunk(batch)
        torch.cuda.synchronize()
        print(f"avg latency: {(time.time() - t0) / N * 1000:.2f} ms")

    print("smoke test passed.")
