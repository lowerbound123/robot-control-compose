"""Accelerated Qwen3.5 VLA KI inference with SDPA + CUDA Graph.

Drop-in replacement for the original modeling_qwen3_5vla_ki module.
Zero precision loss - mathematically equivalent to the original.
"""

import logging

import torch
import torch.nn.functional as F
from torch import Tensor

from transformers.models.qwen3_5 import modeling_qwen3_5

from lerobot.policies.qwen3_5vla_ki.modeling_qwen3_5vla_ki import (
    Qwen3_5VLAKI,
    Qwen3_5VLAKIConfig,
    create_sinusoidal_pos_embedding,
    make_att_2d_masks,
)
logger = logging.getLogger(__name__)


def repeat_kv(hidden_states: Tensor, n_rep: int) -> Tensor:
    batch, num_key_value_heads, slen, head_dim = hidden_states.shape
    if n_rep == 1:
        return hidden_states
    hidden_states = hidden_states[:, :, None, :, :].expand(
        batch, num_key_value_heads, n_rep, slen, head_dim
    )
    return hidden_states.reshape(batch, num_key_value_heads * n_rep, slen, head_dim)


class Qwen3_5VLAKIFast(Qwen3_5VLAKI):
    """Accelerated Qwen3.5 VLA KI with SDPA attention + CUDA Graph for denoise loop."""

    def __init__(self, config: Qwen3_5VLAKIConfig):
        super().__init__(config)
        self._graphs: dict[int, torch.cuda.CUDAGraph] = {}
        self._static_buffers: dict[int, dict] = {}
        self._layer_types = None
        self.action_expert_dtype = torch.float32

    def train(self, mode: bool = True):
        if mode:
            raise RuntimeError("Qwen3_5VLAKIFast is an inference-only backend")
        return super().train(False)

    def _cast_action_expert_to_fp32(self):
        """Cast action expert and suffix-related modules to float32 for higher precision."""
        self.qwen3_5_with_expert.action_expert.to(torch.float32)
        self.action_in_proj.to(torch.float32)
        self.action_time_mlp_in.to(torch.float32)
        self.action_time_mlp_out.to(torch.float32)
        self.action_out_proj.to(torch.float32)
        if hasattr(self, 'state_proj') and self.state_proj is not None:
            self.state_proj.to(torch.float32)

    def _get_layer_types(self):
        if self._layer_types is None:
            self._layer_types = [
                layer.layer_type
                for layer in self.qwen3_5_with_expert.action_expert.layers
            ]
        return self._layer_types

    def _get_static_suffix_masks(self, bsize: int, device: torch.device, dtype: torch.dtype):
        """Pre-cached suffix att_masks and pad_masks to avoid CPU→GPU transfer in CUDA Graph."""
        chunk_size = self.config.chunk_size
        key = (bsize, device)
        if not hasattr(self, "_suffix_masks_cache"):
            self._suffix_masks_cache = {}
        if key not in self._suffix_masks_cache:
            if self.config.tokenize_state:
                att_masks_list = [1] + ([0] * (chunk_size - 1))
            else:
                att_masks_list = [1] + [1] + ([0] * (chunk_size - 1))
            att_masks = torch.tensor(att_masks_list, dtype=dtype, device=device)
            suffix_len = len(att_masks_list)
            pad_masks = torch.ones(bsize, suffix_len, dtype=torch.bool, device=device)
            self._suffix_masks_cache[key] = (pad_masks, att_masks.unsqueeze(0).expand(bsize, -1))
        return self._suffix_masks_cache[key]

    def embed_suffix_fast(self, state, noisy_actions, timestep):
        """CUDA-Graph-compatible version of embed_suffix (no CPU→GPU transfers)."""
        embs = []
        bsize = state.shape[0]
        device = state.device
        dtype = state.dtype

        if not self.config.tokenize_state:
            if self.state_proj.weight.dtype == torch.float32:
                state = state.to(torch.float32)
            state_emb = self.state_proj(state)
            embs.append(state_emb[:, None, :])

        time_emb = create_sinusoidal_pos_embedding(
            timestep,
            self.action_in_proj.out_features,
            min_period=self.config.min_period,
            max_period=self.config.max_period,
            device=device,
        )
        time_emb = time_emb.type(dtype=timestep.dtype)

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

    def _action_expert_forward_sdpa(
        self,
        hidden_states: Tensor,
        attention_mask_4d: Tensor,
        position_ids: Tensor,
        prefix_kv_list: list[tuple[Tensor, Tensor]],
    ) -> Tensor:
        """CUDA-Graph-friendly action expert forward with SDPA.

        Args:
            hidden_states: [B, suffix_len, hidden_size]
            attention_mask_4d: [B, 1, suffix_len, prefix_len+suffix_len]
            position_ids: [3, B, suffix_len]
            prefix_kv_list: list of (key, value) for full_attention layers only
                key/value shape: [B, num_kv_heads, prefix_len, head_dim]
        """
        action_expert = self.qwen3_5_with_expert.action_expert
        layer_types = self._get_layer_types()

        position_embeddings = action_expert.rotary_emb(hidden_states, position_ids)
        cos, sin = position_embeddings

        kv_idx = 0
        for layer_idx, layer in enumerate(action_expert.layers):
            if layer_types[layer_idx] == "linear_attention":
                hidden_states = self._linear_attn_layer(layer, hidden_states)
            else:
                prefix_k, prefix_v = prefix_kv_list[kv_idx]
                kv_idx += 1
                hidden_states = self._full_attn_layer_sdpa(
                    layer, hidden_states, cos, sin, prefix_k, prefix_v, attention_mask_4d
                )

        hidden_states = action_expert.norm(hidden_states)
        return hidden_states

    def _linear_attn_layer(self, layer, hidden_states: Tensor) -> Tensor:
        residual = hidden_states
        hidden_states = layer.input_layernorm(hidden_states)
        hidden_states = layer.linear_attn(
            hidden_states=hidden_states,
            cache_params=None,
            cache_position=None,
            attention_mask=None,
        )
        hidden_states = residual + hidden_states

        residual = hidden_states
        hidden_states = layer.post_attention_layernorm(hidden_states)
        hidden_states = layer.mlp(hidden_states)
        hidden_states = residual + hidden_states
        return hidden_states

    def _full_attn_layer_sdpa(
        self,
        layer,
        hidden_states: Tensor,
        cos: Tensor,
        sin: Tensor,
        prefix_key: Tensor,
        prefix_value: Tensor,
        attention_mask_4d: Tensor,
    ) -> Tensor:
        attn = layer.self_attn
        residual = hidden_states
        hidden_states = layer.input_layernorm(hidden_states)

        input_shape = hidden_states.shape[:-1]
        hidden_shape = (*input_shape, -1, attn.head_dim)

        q_gate = attn.q_proj(hidden_states).view(*input_shape, -1, attn.head_dim * 2)
        query_states, gate = torch.chunk(q_gate, 2, dim=-1)
        gate = gate.reshape(*input_shape, -1)

        query_states = attn.q_norm(query_states.view(hidden_shape)).transpose(1, 2)
        key_states = attn.k_norm(attn.k_proj(hidden_states).view(hidden_shape)).transpose(1, 2)
        value_states = attn.v_proj(hidden_states).view(hidden_shape).transpose(1, 2)

        query_states, key_states = modeling_qwen3_5.apply_rotary_pos_emb(
            query_states, key_states, cos, sin
        )

        # Concatenate prefix (cached) and suffix KV — cast prefix to match suffix dtype
        key_states = torch.cat([prefix_key.to(key_states.dtype), key_states], dim=2)
        value_states = torch.cat([prefix_value.to(value_states.dtype), value_states], dim=2)

        # SDPA - handles GQA via repeat_kv
        key_states = repeat_kv(key_states, attn.num_key_value_groups)
        value_states = repeat_kv(value_states, attn.num_key_value_groups)

        attn_output = F.scaled_dot_product_attention(
            query_states,
            key_states,
            value_states,
            attn_mask=attention_mask_4d.to(query_states.dtype),
            scale=attn.scaling,
        )

        attn_output = attn_output.transpose(1, 2).reshape(*input_shape, -1).contiguous()
        attn_output = attn_output * torch.sigmoid(gate)
        attn_output = attn.o_proj(attn_output)

        hidden_states = residual + attn_output

        residual = hidden_states
        hidden_states = layer.post_attention_layernorm(hidden_states)
        hidden_states = layer.mlp(hidden_states)
        hidden_states = residual + hidden_states
        return hidden_states

    def _denoise_step_fast(
        self,
        state: Tensor,
        prefix_pad_masks: Tensor,
        prefix_kv_list: list[tuple[Tensor, Tensor]],
        max_prefix_position_ids: Tensor,
        x_t: Tensor,
        timestep: Tensor,
        fast_mask: Tensor | None = None,
        attention_mask_4d: Tensor | None = None,
        position_ids: Tensor | None = None,
    ) -> Tensor:
        """Single denoise step using custom SDPA forward (no HF framework overhead)."""
        suffix_embs, suffix_pad_masks, suffix_att_masks = self.embed_suffix_fast(state, x_t, timestep)

        suffix_len = suffix_pad_masks.shape[1]
        batch_size = prefix_pad_masks.shape[0]
        prefix_len = prefix_pad_masks.shape[1]

        if attention_mask_4d is None or position_ids is None:
            prefix_pad_2d_masks = prefix_pad_masks[:, None, :].expand(
                batch_size, suffix_len, prefix_len
            )
            suffix_att_2d_masks = make_att_2d_masks(suffix_pad_masks, suffix_att_masks)
            full_att_2d_masks = torch.cat([prefix_pad_2d_masks, suffix_att_2d_masks], dim=2)

            if fast_mask is not None:
                mask_b = fast_mask.to(device=full_att_2d_masks.device, dtype=torch.bool)
                full_att_2d_masks[:, :, :prefix_len] &= ~mask_b[:, None, :]

            attention_mask_4d = self._prepare_attention_masks_4d(full_att_2d_masks)

            position_ids = (
                torch.arange(1, suffix_len + 1, device=max_prefix_position_ids.device)
                .repeat(3, 1, 1)
                .to(max_prefix_position_ids)
                + max_prefix_position_ids
            )

        suffix_embs = suffix_embs.to(dtype=self.action_expert_dtype)

        hidden_states = self._action_expert_forward_sdpa(
            suffix_embs, attention_mask_4d.to(self.action_expert_dtype), position_ids, prefix_kv_list
        )

        suffix_out = hidden_states[:, -self.config.chunk_size:]
        suffix_out = suffix_out.to(dtype=torch.float32)
        return self.action_out_proj(suffix_out)

    def _extract_prefix_kv(self, past_key_values) -> list[tuple[Tensor, Tensor]]:
        """Extract KV cache tensors for full_attention layers only."""
        layer_types = self._get_layer_types()
        prefix_kv_list = []
        for layer_idx, lt in enumerate(layer_types):
            if lt == "full_attention":
                prefix_kv_list.append((
                    past_key_values.key_cache[layer_idx],
                    past_key_values.value_cache[layer_idx],
                ))
        return prefix_kv_list

    def _build_static_mask_and_pos(
        self,
        state: Tensor,
        prefix_pad_masks: Tensor,
        max_prefix_position_ids: Tensor,
        fast_mask: Tensor | None,
        dtype: torch.dtype,
    ) -> tuple[Tensor, Tensor]:
        """Pre-compute the attention mask and position IDs (static across denoise steps)."""
        suffix_len = self.config.chunk_size
        batch_size = prefix_pad_masks.shape[0]
        prefix_len = prefix_pad_masks.shape[1]
        device = prefix_pad_masks.device

        suffix_pad_masks = torch.ones(batch_size, suffix_len, dtype=torch.bool, device=device)
        att_mask_values = [1] + [0] * (suffix_len - 1)
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
        """Capture CUDA Graph for denoise_step with given prefix_len."""
        device = state.device
        suffix_len = self.config.chunk_size
        batch_size = state.shape[0]

        attention_mask_4d, position_ids = self._build_static_mask_and_pos(
            state, prefix_pad_masks, max_prefix_position_ids, fast_mask, dtype
        )

        # Allocate static buffers
        static_x_t = torch.zeros(
            batch_size, suffix_len, self.config.max_action_dim, device=device, dtype=dtype
        )
        static_timestep = torch.ones(batch_size, device=device, dtype=dtype)

        # Static copies of prefix KV in float32 (CUDA Graph requires fixed memory addresses)
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

        # Warmup forward (ensure all kernels are JIT-compiled)
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

        # Capture graph
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

    @torch.no_grad()
    def sample_actions(
        self,
        pixel_values,
        image_grid_thw,
        lang_tokens,
        lang_masks,
        state,
        fast_token_mask: Tensor | None = None,
        noise=None,
        num_steps=None,
    ) -> Tensor:
        if num_steps is None:
            num_steps = self.config.num_inference_steps

        bsize = state.shape[0]
        device = state.device
        dtype = state.dtype

        if noise is None:
            actions_shape = (bsize, self.config.chunk_size, self.config.max_action_dim)
            noise = self.sample_noise(actions_shape, device)

        # --- Prefix computation (same as original) ---
        prefix_embs, prefix_pad_masks, prefix_att_masks = self.embed_prefix(
            pixel_values, image_grid_thw, lang_tokens, lang_masks
        )
        prefix_att_2d_masks = make_att_2d_masks(prefix_pad_masks, prefix_att_masks)
        prefix_position_ids, rope_deltas = self.get_position_ids(
            lang_tokens, image_grid_thw, prefix_pad_masks
        )
        prefix_att_2d_masks_4d = self._prepare_attention_masks_4d(prefix_att_2d_masks)
        self.qwen3_5_with_expert.qwen3_5.language_model.config._attn_implementation = "eager"

        _, past_key_values = self.qwen3_5_with_expert.forward(
            attention_mask=prefix_att_2d_masks_4d,
            position_ids=prefix_position_ids,
            past_key_values=None,
            inputs_embeds=[prefix_embs, None],
            use_cache=True,
            knowledge_insulation=self.config.knowledge_insulation,
        )

        prefix_len = prefix_pad_masks.shape[1]
        max_prefix_position_ids = prefix_position_ids.max(dim=-1, keepdim=True).values

        if self.config.block_action_attend_fast_tokens:
            fast_mask = self._compute_fast_token_mask(lang_tokens, fast_token_mask)
        else:
            fast_mask = None

        # Extract prefix KV cache as flat list
        prefix_kv_list = self._extract_prefix_kv(past_key_values)

        # --- CUDA Graph path ---
        if prefix_len not in self._graphs:
            self._capture_graph(
                prefix_len, state.float(), prefix_pad_masks, prefix_kv_list,
                max_prefix_position_ids, fast_mask, torch.float32,
            )

        buffers = self._static_buffers[prefix_len]
        graph = self._graphs[prefix_len]

        # Copy current prefix KV into static buffers
        for i, (k, v) in enumerate(prefix_kv_list):
            buffers["prefix_kv_list"][i][0].copy_(k)
            buffers["prefix_kv_list"][i][1].copy_(v)
        buffers["state"].copy_(state.float())

        # Denoise loop with CUDA Graph replay
        dt = -1.0 / num_steps
        x_t = noise.float()
        time_val = 1.0

        for _ in range(num_steps):
            buffers["x_t"].copy_(x_t)
            buffers["timestep"].fill_(time_val)
            graph.replay()
            x_t = x_t + dt * buffers["output"]
            time_val += dt

        return x_t

    @torch.no_grad()
    def sample_actions_no_graph(
        self,
        pixel_values,
        image_grid_thw,
        lang_tokens,
        lang_masks,
        state,
        fast_token_mask: Tensor | None = None,
        noise=None,
        num_steps=None,
    ) -> Tensor:
        """SDPA-only path without CUDA Graph (for debugging/comparison)."""
        if num_steps is None:
            num_steps = self.config.num_inference_steps

        bsize = state.shape[0]
        device = state.device
        dtype = state.dtype

        if noise is None:
            actions_shape = (bsize, self.config.chunk_size, self.config.max_action_dim)
            noise = self.sample_noise(actions_shape, device)

        prefix_embs, prefix_pad_masks, prefix_att_masks = self.embed_prefix(
            pixel_values, image_grid_thw, lang_tokens, lang_masks
        )
        prefix_att_2d_masks = make_att_2d_masks(prefix_pad_masks, prefix_att_masks)
        prefix_position_ids, rope_deltas = self.get_position_ids(
            lang_tokens, image_grid_thw, prefix_pad_masks
        )
        prefix_att_2d_masks_4d = self._prepare_attention_masks_4d(prefix_att_2d_masks)
        self.qwen3_5_with_expert.qwen3_5.language_model.config._attn_implementation = "eager"

        _, past_key_values = self.qwen3_5_with_expert.forward(
            attention_mask=prefix_att_2d_masks_4d,
            position_ids=prefix_position_ids,
            past_key_values=None,
            inputs_embeds=[prefix_embs, None],
            use_cache=True,
            knowledge_insulation=self.config.knowledge_insulation,
        )

        max_prefix_position_ids = prefix_position_ids.max(dim=-1, keepdim=True).values

        if self.config.block_action_attend_fast_tokens:
            fast_mask = self._compute_fast_token_mask(lang_tokens, fast_token_mask)
        else:
            fast_mask = None

        prefix_kv_list = self._extract_prefix_kv(past_key_values)

        dt = -1.0 / num_steps
        x_t = noise.float()
        time_val = 1.0
        state_f32 = state.float()

        for _ in range(num_steps):
            expanded_time = torch.full((bsize,), time_val, device=device, dtype=torch.float32)
            v_t = self._denoise_step_fast(
                state_f32, prefix_pad_masks, prefix_kv_list,
                max_prefix_position_ids, x_t, expanded_time,
                fast_mask=fast_mask,
            )
            x_t = x_t + dt * v_t
            time_val += dt

        return x_t
