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

import builtins
import contextlib
import io
import logging
import math
from collections import deque
from pathlib import Path
from typing import Literal

import torch
import torch.nn.functional as F  # noqa: N812
import torch._dynamo as dynamo
from torch import Tensor, nn
from transformers import AutoTokenizer
from transformers.models.auto import CONFIG_MAPPING
from transformers.models.qwen3_5 import (
    Qwen3_5Config,
    Qwen3_5ForConditionalGeneration,
    Qwen3_5TextModel,
    Qwen3_5Tokenizer,
    modeling_qwen3_5,
)

from lerobot.policies.pretrained import PreTrainedPolicy, T
from lerobot.policies.qwen3_5vla_ki.action_tokens import ensure_qwen35_action_tokens
from lerobot.policies.qwen3_5vla_ki.configuration_qwen3_5vla_ki import Qwen3_5VLAKIConfig
from lerobot.utils.constants import (
    ACTION,
    OBS_PREFIX,
    OBS_STATE,
    OPENPI_ATTENTION_MASK_VALUE,
)
from lerobot.utils.loss_utils import build_loss_metrics, split_vlm_token_loss
from lerobot.utils.utils import format_big_number


def get_safe_dtype(target_dtype, device_type):
    """Get a safe dtype for the given device type."""
    if device_type == "mps" and target_dtype == torch.float64:
        return torch.float32
    if device_type == "cpu":
        if target_dtype == torch.bfloat16:
            return torch.float32
        if target_dtype == torch.float64:
            return torch.float64
    return target_dtype


def create_sinusoidal_pos_embedding(
    time: torch.Tensor, dimension: int, min_period: float, max_period: float, device="cpu"
) -> Tensor:
    """Computes sine-cosine positional embedding vectors for scalar positions."""
    if dimension % 2 != 0:
        raise ValueError(f"dimension ({dimension}) must be divisible by 2")

    if time.ndim != 1:
        raise ValueError("The time tensor is expected to be of shape `(batch_size, )`.")

    dtype = get_safe_dtype(torch.float64, device.type)
    fraction = torch.linspace(0.0, 1.0, dimension // 2, dtype=dtype, device=device)
    period = min_period * (max_period / min_period) ** fraction

    scaling_factor = 1.0 / period * 2 * math.pi
    sin_input = scaling_factor[None, :] * time[:, None]
    return torch.cat([torch.sin(sin_input), torch.cos(sin_input)], dim=1)


def sample_beta(alpha, beta, bsize, device):
    alpha_t = torch.as_tensor(alpha, dtype=torch.float32, device=device)
    beta_t = torch.as_tensor(beta, dtype=torch.float32, device=device)
    dist = torch.distributions.Beta(alpha_t, beta_t)
    return dist.sample((bsize,))


def make_att_2d_masks(pad_masks, att_masks):
    """Copied from big_vision."""
    if att_masks.ndim != 2:
        raise ValueError(att_masks.ndim)
    if pad_masks.ndim != 2:
        raise ValueError(pad_masks.ndim)

    cumsum = torch.cumsum(att_masks, dim=1)
    att_2d_masks = cumsum[:, None, :] <= cumsum[:, :, None]
    pad_2d_masks = pad_masks[:, None, :] * pad_masks[:, :, None]
    return att_2d_masks & pad_2d_masks


def pad_vector(vector, new_dim):
    if vector.shape[-1] >= new_dim:
        return vector
    return F.pad(vector, (0, new_dim - vector.shape[-1]))


def compute_layer_complete(
    layer_idx,
    inputs_embeds,
    attention_mask,
    position_ids,
    qwen3_5,
    action_expert,
    prefix_len: int,
    knowledge_insulation: bool = False,
    use_sdpa: bool = False,
    linear_attn_mask: torch.Tensor | None = None,
):
    """Run one transformer layer jointly on [VLM prefix, action expert suffix].

    When ``knowledge_insulation`` is True, the suffix (action expert) queries
    attend to the prefix keys/values with gradient detached, so gradients from
    the action / suffix branch cannot flow back into the VLM.

    ``linear_attention`` layers are processed fully independently per model –
    their recurrent state cannot be shared – so KI is naturally enforced there.

    Args:
        linear_attn_mask: 2D [B, total_len] padding mask for linear attention
            layers (1=real, 0=padding/masked). If None, no masking is applied.
    """

    models = [qwen3_5.language_model, action_expert]
    layer_type = qwen3_5.language_model.layers[layer_idx].layer_type

    if layer_type == "linear_attention":
        if linear_attn_mask is not None:
            prefix_linear_mask = linear_attn_mask[:, :prefix_len]
            suffix_linear_mask = linear_attn_mask[:, prefix_len:]
            linear_masks_per_model = [prefix_linear_mask, suffix_linear_mask]
        else:
            linear_masks_per_model = [None, None]

        outputs_embeds = []
        for i, hidden_states in enumerate(inputs_embeds):
            layer = models[i].layers[layer_idx]

            residual = hidden_states
            hidden_states = layer.input_layernorm(hidden_states)
            hidden_states = layer.linear_attn(
                hidden_states=hidden_states,
                cache_params=None,
                cache_position=None,
                # attention_mask=attention_mask,  # BUG: 4D mask causes apply_mask_to_padding_states to silently no-op
                attention_mask=linear_masks_per_model[i],
            )
            hidden_states = residual + hidden_states

            after_first_residual = hidden_states
            hidden_states = layer.post_attention_layernorm(hidden_states)

            if layer.mlp.up_proj.weight.dtype == torch.bfloat16:
                hidden_states = hidden_states.to(dtype=torch.bfloat16)
            hidden_states = layer.mlp(hidden_states)

            hidden_states = hidden_states + after_first_residual
            outputs_embeds.append(hidden_states)

        return outputs_embeds

    elif layer_type == "full_attention":
        # Compute Q/K/V/gate for each branch separately.
        query_states = []
        key_states = []
        value_states = []
        gates = []
        for i, hidden_states in enumerate(inputs_embeds):
            layer = models[i].layers[layer_idx]
            hidden_states = layer.input_layernorm(hidden_states)
            input_shape = hidden_states.shape[:-1]

            q_gate = layer.self_attn.q_proj(hidden_states).view(
                *input_shape, -1, layer.self_attn.head_dim * 2
            )
            query_state, gate = torch.chunk(q_gate, 2, dim=-1)
            gate = gate.reshape(*input_shape, -1)

            hidden_shape = (*input_shape, -1, layer.self_attn.head_dim)
            query_state = layer.self_attn.q_norm(query_state.view(hidden_shape)).transpose(1, 2)
            key_state = layer.self_attn.k_norm(
                layer.self_attn.k_proj(hidden_states).view(hidden_shape)
            ).transpose(1, 2)
            value_state = layer.self_attn.v_proj(hidden_states).view(hidden_shape).transpose(1, 2)

            query_states.append(query_state)
            key_states.append(key_state)
            value_states.append(value_state)
            gates.append(gate)

        prefix_query, suffix_query = query_states
        prefix_key, suffix_key = key_states
        prefix_value, suffix_value = value_states
        prefix_gate, suffix_gate = gates

        # Joint K/V/Q for applying rotary position embedding on the full sequence.
        joint_query = torch.cat(query_states, dim=2)
        joint_key = torch.cat(key_states, dim=2)
        joint_value = torch.cat(value_states, dim=2)

        dummy_tensor = torch.zeros(
            joint_query.shape[0],
            joint_query.shape[2],
            joint_query.shape[-1],
            device=joint_query.device,
            dtype=joint_query.dtype,
        )
        cos, sin = qwen3_5.language_model.rotary_emb(dummy_tensor, position_ids)
        joint_query, joint_key = modeling_qwen3_5.apply_rotary_pos_emb(
            joint_query, joint_key, cos, sin, unsqueeze_dim=1
        )

        # Split back after RoPE.
        prefix_query = joint_query[:, :, :prefix_len]
        suffix_query = joint_query[:, :, prefix_len:]
        prefix_key = joint_key[:, :, :prefix_len]
        suffix_key = joint_key[:, :, prefix_len:]
        prefix_value = joint_value[:, :, :prefix_len]
        suffix_value = joint_value[:, :, prefix_len:]

        scaling = qwen3_5.language_model.layers[layer_idx].self_attn.scaling
        attn_layer = qwen3_5.language_model.layers[layer_idx].self_attn

        batch_size = joint_query.shape[0]

        # --- prefix queries: attend only to prefix K/V (pad mask already
        # forbids prefix -> suffix). Gradient flows normally for VLM.
        prefix_attn_mask = attention_mask[:, :, :prefix_len, :prefix_len]
        if use_sdpa:
            prefix_key_expanded = modeling_qwen3_5.repeat_kv(prefix_key, attn_layer.num_key_value_groups)
            prefix_value_expanded = modeling_qwen3_5.repeat_kv(prefix_value, attn_layer.num_key_value_groups)
            prefix_att_output = F.scaled_dot_product_attention(
                prefix_query, prefix_key_expanded, prefix_value_expanded,
                attn_mask=prefix_attn_mask.to(prefix_query.dtype), scale=scaling,
            )
            prefix_att_output = prefix_att_output.transpose(1, 2).contiguous()
        else:
            prefix_att_output, _ = modeling_qwen3_5.eager_attention_forward(
                attn_layer,
                prefix_query,
                prefix_key,
                prefix_value,
                prefix_attn_mask,
                scaling,
            )

        # --- suffix queries: attend to [prefix (maybe-detached) K/V, suffix K/V].
        if knowledge_insulation:
            prefix_key_for_suffix = prefix_key.detach()
            prefix_value_for_suffix = prefix_value.detach()
        else:
            prefix_key_for_suffix = prefix_key
            prefix_value_for_suffix = prefix_value

        k_for_suffix = torch.cat([prefix_key_for_suffix, suffix_key], dim=2)
        v_for_suffix = torch.cat([prefix_value_for_suffix, suffix_value], dim=2)
        suffix_attn_mask = attention_mask[:, :, prefix_len:, :]

        if use_sdpa:
            k_for_suffix_expanded = modeling_qwen3_5.repeat_kv(k_for_suffix, attn_layer.num_key_value_groups)
            v_for_suffix_expanded = modeling_qwen3_5.repeat_kv(v_for_suffix, attn_layer.num_key_value_groups)
            suffix_att_output = F.scaled_dot_product_attention(
                suffix_query, k_for_suffix_expanded, v_for_suffix_expanded,
                attn_mask=suffix_attn_mask.to(suffix_query.dtype), scale=scaling,
            )
            suffix_att_output = suffix_att_output.transpose(1, 2).contiguous()
        else:
            suffix_att_output, _ = modeling_qwen3_5.eager_attention_forward(
                attn_layer,
                suffix_query,
                k_for_suffix,
                v_for_suffix,
                suffix_attn_mask,
                scaling,
            )

        att_output = torch.cat([prefix_att_output, suffix_att_output], dim=1)

        head_dim = qwen3_5.language_model.layers[layer_idx].self_attn.head_dim
        num_attention_heads = qwen3_5.language_model.layers[layer_idx].self_attn.config.num_attention_heads
        att_output = att_output.reshape(batch_size, -1, num_attention_heads * head_dim)

        gates_joint = torch.cat(gates, dim=1)

        outputs_embeds = []
        start_pos = 0
        for i, hidden_states in enumerate(inputs_embeds):
            layer = models[i].layers[layer_idx]
            end_pos = start_pos + hidden_states.shape[1]

            att_out_slice = att_output[:, start_pos:end_pos]
            gate_slice = gates_joint[:, start_pos:end_pos]

            att_out_slice = att_out_slice * torch.sigmoid(gate_slice)

            if att_out_slice.dtype != layer.self_attn.o_proj.weight.dtype:
                att_out_slice = att_out_slice.to(layer.self_attn.o_proj.weight.dtype)
            out_emb = layer.self_attn.o_proj(att_out_slice)

            out_emb = out_emb + hidden_states
            after_first_residual = out_emb.clone()
            out_emb = layer.post_attention_layernorm(out_emb)

            if layer.mlp.up_proj.weight.dtype == torch.bfloat16:
                out_emb = out_emb.to(dtype=torch.bfloat16)
            out_emb = layer.mlp(out_emb)

            out_emb = out_emb + after_first_residual
            outputs_embeds.append(out_emb)
            start_pos = end_pos
        return outputs_embeds

    else:
        raise ValueError(f"Unknown layer_type: {layer_type}")


class ActionExpertConfig:
    """Configuration for the action expert module.

    Note: head_dim is inherited from VLM for proper cross-attention computation.
    Only hidden_size and intermediate_size can be customized.
    """

    def __init__(
        self,
        hidden_size: int | None = None,
        intermediate_size: int | None = None,
        head_dim: int | None = None,
        num_attention_heads: int | None = None,
        num_key_value_heads: int | None = None,
    ):
        self.head_dim = head_dim
        self.hidden_size = hidden_size
        self.intermediate_size = intermediate_size
        self.num_attention_heads = num_attention_heads
        self.num_key_value_heads = num_key_value_heads


class Qwen3_5WithExpertModel(nn.Module):
    """Qwen3_5 model with action expert for Qwen3_5VLAKI."""

    def __init__(
        self,
        vlm_model_name_or_path: str = "Qwen/Qwen3.5-2B",
        action_expert_config: ActionExpertConfig | None = None,
        precision: Literal["bfloat16", "float32"] = "bfloat16",
    ):
        super().__init__()

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.qwen3_5 = Qwen3_5ForConditionalGeneration.from_pretrained(vlm_model_name_or_path)
            tokenizer = Qwen3_5Tokenizer.from_pretrained(vlm_model_name_or_path)
            ensure_qwen35_action_tokens(tokenizer, self.qwen3_5)

        vlm_text_config = self.qwen3_5.config.text_config

        if action_expert_config is None:
            action_expert_config = ActionExpertConfig()

        if action_expert_config.head_dim is None:
            action_expert_config.head_dim = vlm_text_config.head_dim
        if action_expert_config.hidden_size is None:
            action_expert_config.hidden_size = vlm_text_config.hidden_size
        if action_expert_config.intermediate_size is None:
            action_expert_config.intermediate_size = vlm_text_config.intermediate_size

        action_expert_config.num_attention_heads = vlm_text_config.num_attention_heads
        action_expert_config.num_key_value_heads = vlm_text_config.num_key_value_heads

        action_expert_config_hf = CONFIG_MAPPING["qwen3_5_text"]()
        action_expert_config_hf.head_dim = action_expert_config.head_dim
        action_expert_config_hf.hidden_size = action_expert_config.hidden_size
        action_expert_config_hf.intermediate_size = action_expert_config.intermediate_size
        action_expert_config_hf.num_attention_heads = action_expert_config.num_attention_heads
        action_expert_config_hf.num_key_value_heads = action_expert_config.num_key_value_heads
        action_expert_config_hf.num_hidden_layers = vlm_text_config.num_hidden_layers
        action_expert_config_hf.max_position_embeddings = vlm_text_config.max_position_embeddings
        action_expert_config_hf.rope_parameters = vlm_text_config.rope_parameters
        action_expert_config_hf.rms_norm_eps = vlm_text_config.rms_norm_eps

        action_expert_config_hf.layer_types = vlm_text_config.layer_types
        action_expert_config_hf.linear_conv_kernel_dim = vlm_text_config.linear_conv_kernel_dim
        action_expert_config_hf.linear_key_head_dim = vlm_text_config.linear_key_head_dim
        action_expert_config_hf.linear_value_head_dim = vlm_text_config.linear_value_head_dim
        action_expert_config_hf.linear_num_key_heads = vlm_text_config.linear_num_key_heads
        action_expert_config_hf.linear_num_value_heads = vlm_text_config.linear_num_value_heads

        self.action_expert = Qwen3_5TextModel(config=action_expert_config_hf)
        self.action_expert.embed_tokens = None

        self.to_bfloat16_for_selected_params(precision)

    def to_bfloat16_for_selected_params(
        self, precision: Literal["bfloat16", "float32"] = "bfloat16"
    ):
        if precision == "bfloat16":
            self.to(dtype=torch.bfloat16)
        elif precision == "float32":
            self.to(dtype=torch.float32)
            return
        else:
            raise ValueError(f"Invalid precision: {precision}")

        params_to_keep_float32 = [
            "input_layernorm",
            "post_attention_layernorm",
            "model.norm",
        ]

        for name, param in self.named_parameters():
            if any(selector in name for selector in params_to_keep_float32):
                param.data = param.data.to(dtype=torch.float32)

    def forward(
        self,
        attention_mask: torch.Tensor | None = None,
        position_ids: torch.LongTensor | None = None,
        past_key_values: list[torch.FloatTensor] | None = None,
        inputs_embeds: list[torch.FloatTensor] | None = None,
        use_cache: bool | None = None,
        knowledge_insulation: bool = False,
        use_sdpa: bool = False,
        linear_attn_mask: torch.Tensor | None = None,
    ):
        if inputs_embeds[1] is None:
            prefix_output = self.qwen3_5.language_model.forward(
                inputs_embeds=inputs_embeds[0],
                attention_mask=attention_mask,
                position_ids=position_ids,
                past_key_values=past_key_values,
                use_cache=use_cache,
            )
            prefix_past_key_values = prefix_output.past_key_values
            prefix_output = prefix_output.last_hidden_state
            suffix_output = None
        elif inputs_embeds[0] is None:
            suffix_output = self.action_expert.forward(
                inputs_embeds=inputs_embeds[1],
                attention_mask=attention_mask,
                position_ids=position_ids,
                past_key_values=past_key_values,
                use_cache=use_cache,
            )
            suffix_output = suffix_output.last_hidden_state
            prefix_output = None
            prefix_past_key_values = None
        else:
            models = [self.qwen3_5.language_model, self.action_expert]
            num_layers = self.qwen3_5.config.text_config.num_hidden_layers

            prefix_len = inputs_embeds[0].shape[1]

            use_gradient_checkpointing = (
                hasattr(self.action_expert, "gradient_checkpointing")
                and self.action_expert.gradient_checkpointing
                and self.training
            ) or (
                hasattr(self, "gradient_checkpointing")
                and self.gradient_checkpointing
                and self.training
            )

            for layer_idx in range(num_layers):
                if use_gradient_checkpointing:
                    inputs_embeds = torch.utils.checkpoint.checkpoint(
                        compute_layer_complete,
                        layer_idx,
                        inputs_embeds,
                        attention_mask,
                        position_ids,
                        self.qwen3_5,
                        self.action_expert,
                        prefix_len,
                        knowledge_insulation,
                        use_sdpa,
                        linear_attn_mask,
                        use_reentrant=False,
                        preserve_rng_state=False,
                    )
                else:
                    inputs_embeds = compute_layer_complete(
                        layer_idx,
                        inputs_embeds,
                        attention_mask,
                        position_ids,
                        qwen3_5=self.qwen3_5,
                        action_expert=self.action_expert,
                        prefix_len=prefix_len,
                        knowledge_insulation=knowledge_insulation,
                        use_sdpa=use_sdpa,
                        linear_attn_mask=linear_attn_mask,
                    )

            def compute_final_norms(inputs_embeds):
                outputs_embeds = []
                for i, hidden_states in enumerate(inputs_embeds):
                    out_emb = models[i].norm(hidden_states)
                    outputs_embeds.append(out_emb)
                return outputs_embeds

            if use_gradient_checkpointing:
                outputs_embeds = torch.utils.checkpoint.checkpoint(
                    compute_final_norms,
                    inputs_embeds,
                    use_reentrant=False,
                    preserve_rng_state=False,
                )
            else:
                outputs_embeds = compute_final_norms(inputs_embeds)

            prefix_output = outputs_embeds[0]
            suffix_output = outputs_embeds[1]
            prefix_past_key_values = None

        return [prefix_output, suffix_output], prefix_past_key_values


class Qwen3_5VLAKI(nn.Module):

    def __init__(self, config: Qwen3_5VLAKIConfig):
        super().__init__()
        self.config = config
        self._vqa_debug_step = 0
        self._vqa_debug_tokenizer = None

        action_expert_config = ActionExpertConfig(
            hidden_size=config.action_expert_hidden_size,
            intermediate_size=config.action_expert_intermediate_size,
        )

        self.qwen3_5_with_expert = Qwen3_5WithExpertModel(
            vlm_model_name_or_path=config.vlm_model_name_or_path,
            action_expert_config=action_expert_config,
            precision=config.dtype,
        )

        action_expert_hidden_size = self.qwen3_5_with_expert.action_expert.config.hidden_size

        self.action_in_proj = nn.Linear(config.max_action_dim, action_expert_hidden_size)
        self.action_out_proj = nn.Linear(action_expert_hidden_size, config.max_action_dim)

        if not self.config.tokenize_state:
            self.state_proj = nn.Linear(config.max_state_dim, action_expert_hidden_size)

        self.action_time_mlp_in = nn.Linear(2 * action_expert_hidden_size, action_expert_hidden_size)
        self.action_time_mlp_out = nn.Linear(action_expert_hidden_size, action_expert_hidden_size)

        self.gradient_checkpointing_enabled = False

        if config.compile_model:
            torch.set_float32_matmul_precision("high")
            self.sample_actions = torch.compile(self.sample_actions, mode=config.compile_mode)
            self.forward = torch.compile(self.forward, dynamic=True, mode=config.compile_mode)

        self.set_requires_grad()

    def set_requires_grad(self):
        if self.config.freeze_vision_encoder:
            self.qwen3_5_with_expert.qwen3_5.visual.eval()
            for params in self.qwen3_5_with_expert.qwen3_5.visual.parameters():
                params.requires_grad = False

        if self.config.train_expert_only:
            self.qwen3_5_with_expert.qwen3_5.eval()
            for params in self.qwen3_5_with_expert.qwen3_5.parameters():
                params.requires_grad = False

    def train(self, mode: bool = True):
        super().train(mode)

        if self.config.freeze_vision_encoder:
            self.qwen3_5_with_expert.qwen3_5.visual.eval()

        if self.config.train_expert_only:
            self.qwen3_5_with_expert.qwen3_5.eval()

        return self

    def gradient_checkpointing_enable(self):
        self.gradient_checkpointing_enabled = True
        self.qwen3_5_with_expert.qwen3_5.language_model.gradient_checkpointing = True
        self.qwen3_5_with_expert.qwen3_5.visual.gradient_checkpointing = True
        self.qwen3_5_with_expert.action_expert.gradient_checkpointing = True
        logging.info("Enabled gradient checkpointing for Qwen3_5VLAKI model")

    def gradient_checkpointing_disable(self):
        self.gradient_checkpointing_enabled = False
        self.qwen3_5_with_expert.qwen3_5.language_model.gradient_checkpointing = False
        self.qwen3_5_with_expert.qwen3_5.visual.gradient_checkpointing = False
        self.qwen3_5_with_expert.action_expert.gradient_checkpointing = False
        logging.info("Disabled gradient checkpointing for Qwen3_5VLAKI model")

    def _apply_checkpoint(self, func, *args, **kwargs):
        if self.gradient_checkpointing_enabled and self.training:
            return torch.utils.checkpoint.checkpoint(
                func, *args, use_reentrant=False, preserve_rng_state=False, **kwargs
            )
        return func(*args, **kwargs)

    def _prepare_attention_masks_4d(self, att_2d_masks):
        att_2d_masks_4d = att_2d_masks[:, None, :, :]
        return torch.where(att_2d_masks_4d, 0.0, OPENPI_ATTENTION_MASK_VALUE)

    def _compute_fast_token_mask(
        self, lang_tokens: Tensor, provided_mask: Tensor | None = None
    ) -> Tensor:
        """Return a bool mask marking fast-token positions in the prefix.

        Uses the transform-provided mask if available; otherwise derives it from
        the configured ``[action_token_min, action_token_max]`` range.
        """
        if provided_mask is not None:
            return provided_mask.to(device=lang_tokens.device, dtype=torch.bool)
        return (
            (lang_tokens >= self.config.action_token_min)
            & (lang_tokens <= self.config.action_token_max)
        )

    @dynamo.disable
    def _get_vqa_debug_tokenizer(self):
        if self._vqa_debug_tokenizer is None:
            self._vqa_debug_tokenizer = AutoTokenizer.from_pretrained(self.config.vlm_model_name_or_path)
            ensure_qwen35_action_tokens(self._vqa_debug_tokenizer)
        return self._vqa_debug_tokenizer

    @dynamo.disable
    def _maybe_log_vqa_prediction(self, logits: Tensor, labels: Tensor, step: int) -> None:
        if not self.training or dynamo.is_compiling():
            return

        self._vqa_debug_step += 1
        if self._vqa_debug_step % step != 0:
            return

        with torch.no_grad():
            labels_shifted = labels[:, 1:]
            valid_mask = labels_shifted != -100
            valid_sample_indices = valid_mask.any(dim=1).nonzero(as_tuple=False).squeeze(-1)
            if valid_sample_indices.numel() == 0:
                builtins.print(
                    f"[VQA DEBUG] step={self._vqa_debug_step} no valid label tokens",
                    flush=True,
                )
                return

            rand_pos = int(
                torch.randint(valid_sample_indices.numel(), (1,), device=valid_sample_indices.device).item()
            )
            sample_idx = int(valid_sample_indices[rand_pos].item())
            sample_valid_pos = valid_mask[sample_idx].nonzero(as_tuple=False).squeeze(-1)
            show_pos = sample_valid_pos[:32]

            sample_logits = logits[sample_idx, :-1, :]
            pred_ids_show = sample_logits[show_pos].argmax(dim=-1).detach().cpu().tolist()
            gt_ids = labels_shifted[sample_idx, show_pos].detach().cpu().tolist()
            gt_ids = [int(x) for x in gt_ids]
            pred_ids_show = [int(x) for x in pred_ids_show]

            tokenizer = self._get_vqa_debug_tokenizer()
            label_text = tokenizer.decode(gt_ids, skip_special_tokens=True)
            pred_text = tokenizer.decode(pred_ids_show, skip_special_tokens=True)

            builtins.print(
                (
                    f"[VQA DEBUG] step={self._vqa_debug_step} sample={sample_idx} "
                    f"valid_tokens={int(sample_valid_pos.numel())} shown={len(gt_ids)} "
                    f"| label_text={label_text!r} | pred_text={pred_text!r}"
                ),
                flush=True,
            )

    def sample_noise(self, shape, device):
        return torch.normal(
            mean=0.0,
            std=1.0,
            size=shape,
            dtype=torch.float32,
            device=device,
        )

    def sample_time(self, bsize, device):
        time_beta = sample_beta(
            self.config.time_sampling_beta_alpha,
            self.config.time_sampling_beta_beta,
            bsize,
            device,
        )
        time = time_beta * self.config.time_sampling_scale + self.config.time_sampling_offset
        return time.to(dtype=torch.float32, device=device)

    @dynamo.disable
    def embed_prefix(
        self, pixel_values, image_grid_thw, lang_tokens, lang_masks, labels=None
    ) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        image_token_id = self.qwen3_5_with_expert.qwen3_5.config.image_token_id
        D1 = pixel_values.shape[-1]
        pixel_values = pixel_values.view(-1, D1)
        image_grid_thw = image_grid_thw.view(-1, 3)
        image_embs = self.qwen3_5_with_expert.qwen3_5.visual(pixel_values, image_grid_thw).pooler_output

        embs = self.qwen3_5_with_expert.qwen3_5.get_input_embeddings()(lang_tokens)
        B, L, D2 = embs.shape
        embs = embs.view(-1, D2)
        lang_tokens = lang_tokens.view(-1)
        embs[lang_tokens == image_token_id] = image_embs
        embs = embs.view(B, L, D2)

        pad_masks = lang_masks.to(torch.bool)
        # if labels is not None:
        #     att_masks = (labels != -100).to(torch.bool)
        # else:
        #     att_masks = torch.zeros_like(pad_masks, dtype=torch.bool, device=pad_masks.device)
        # Force causal attention over the prefix to match Qwen3.5's pretrained
        # decoder-only LM regime. Each valid position starts its own block, so
        # cumsum becomes 1..L and `cumsum_kv <= cumsum_q` reduces to a standard
        # lower-triangular causal mask. The `labels` argument is kept for API
        # compatibility but no longer alters the prefix attention pattern.
        del labels  # noqa: F841 — intentionally unused
        att_masks = pad_masks.clone()

        return embs, pad_masks, att_masks

    def embed_suffix(self, state, noisy_actions, timestep):
        embs = []
        pad_masks = []
        att_masks = []

        if not self.config.tokenize_state:
            if self.state_proj.weight.dtype == torch.float32:
                state = state.to(torch.float32)

            def state_proj_func(state):
                return self.state_proj(state)

            state_emb = self._apply_checkpoint(state_proj_func, state)
            embs.append(state_emb[:, None, :])
            bsize = state_emb.shape[0]
            device = state_emb.device

            state_mask = torch.ones(bsize, 1, dtype=torch.bool, device=device)
            pad_masks.append(state_mask)
            att_masks += [1]

        bsize = state.shape[0]
        device = state.device
        time_emb = create_sinusoidal_pos_embedding(
            timestep,
            self.action_in_proj.out_features,
            min_period=self.config.min_period,
            max_period=self.config.max_period,
            device=timestep.device,
        )
        time_emb = time_emb.type(dtype=timestep.dtype)

        def action_proj_func(noisy_actions):
            return self.action_in_proj(noisy_actions)

        action_emb = self._apply_checkpoint(action_proj_func, noisy_actions)

        time_emb = time_emb[:, None, :].expand_as(action_emb)
        action_time_emb = torch.cat([action_emb, time_emb], dim=2)

        def mlp_func(action_time_emb):
            x = self.action_time_mlp_in(action_time_emb)
            x = F.silu(x)
            return self.action_time_mlp_out(x)

        action_time_emb = self._apply_checkpoint(mlp_func, action_time_emb)

        embs.append(action_time_emb)
        bsize, action_time_dim = action_time_emb.shape[:2]
        action_time_mask = torch.ones(bsize, action_time_dim, dtype=torch.bool, device=timestep.device)
        pad_masks.append(action_time_mask)

        att_masks += [1] + ([0] * (self.config.chunk_size - 1))

        embs = torch.cat(embs, dim=1)
        pad_masks = torch.cat(pad_masks, dim=1)
        att_masks = torch.tensor(att_masks, dtype=embs.dtype, device=embs.device)
        att_masks = att_masks[None, :].expand(bsize, len(att_masks))

        return embs, pad_masks, att_masks

    def get_position_ids(self, lang_tokens, image_grid_thw, pad_masks):
        L = lang_tokens.shape[1]
        pseudo_action_token_id = 777
        padded_lang_tokens = torch.ones_like(pad_masks).to(lang_tokens) * pseudo_action_token_id
        padded_lang_tokens[:, :L] = lang_tokens
        attention_mask = pad_masks.to(lang_tokens)
        if image_grid_thw is not None:
            image_grid_thw = image_grid_thw.view(-1, 3)
        position_ids, rope_deltas = self.qwen3_5_with_expert.qwen3_5.model.get_rope_index(
            padded_lang_tokens,
            image_grid_thw,
            attention_mask=attention_mask,
        )
        return position_ids, rope_deltas

    def _block_suffix_attend_prefix_tokens(
        self,
        att_2d_masks: Tensor,
        prefix_len: int,
        blocked_prefix_mask: Tensor | None,
    ) -> Tensor:
        """Block suffix (action) queries from attending selected prefix keys."""
        if blocked_prefix_mask is None:
            return att_2d_masks
        if blocked_prefix_mask.dtype != torch.bool:
            blocked_prefix_mask = blocked_prefix_mask.bool()
        blocked_prefix_mask = blocked_prefix_mask.to(device=att_2d_masks.device)
        att_2d_masks[:, prefix_len:, :prefix_len] &= ~blocked_prefix_mask[:, None, :]
        return att_2d_masks

    def forward(
        self,
        pixel_values,
        image_grid_thw,
        lang_tokens,
        lang_masks,
        state,
        actions,
        labels: Tensor | None = None,
        fast_token_mask: Tensor | None = None,
        noise=None,
        time=None,
    ) -> tuple[Tensor, Tensor]:
        """Do a full training forward pass and compute the loss."""
        if noise is None:
            noise = self.sample_noise(actions.shape, actions.device)

        if time is None:
            time = self.sample_time(actions.shape[0], actions.device)

        time_expanded = time[:, None, None]
        x_t = time_expanded * noise + (1 - time_expanded) * actions
        u_t = noise - actions

        prefix_embs, prefix_pad_masks, prefix_att_masks = self.embed_prefix(
            pixel_values, image_grid_thw, lang_tokens, lang_masks, labels
        )
        suffix_embs, suffix_pad_masks, suffix_att_masks = self.embed_suffix(state, x_t, time)

        if (
            self.qwen3_5_with_expert.qwen3_5.language_model.layers[0].mlp.up_proj.weight.dtype
            == torch.bfloat16
        ):
            suffix_embs = suffix_embs.to(dtype=torch.bfloat16)
            prefix_embs = prefix_embs.to(dtype=torch.bfloat16)

        pad_masks = torch.cat([prefix_pad_masks, suffix_pad_masks], dim=1)
        att_masks = torch.cat([prefix_att_masks, suffix_att_masks], dim=1)

        att_2d_masks = make_att_2d_masks(pad_masks, att_masks)

        # Block action expert queries from attending to fast-action tokens.
        if self.config.block_action_attend_fast_tokens:
            fast_mask = self._compute_fast_token_mask(lang_tokens, fast_token_mask)
            att_2d_masks = self._block_suffix_attend_prefix_tokens(
                att_2d_masks=att_2d_masks,
                prefix_len=prefix_pad_masks.shape[1],
                blocked_prefix_mask=fast_mask,
            )
        else:
            fast_mask = None

        # position_ids, rope_deltas = self.get_position_ids(lang_tokens, image_grid_thw, pad_masks)

        # ============ 修改：suffix position_ids 与推理对齐 ============
        prefix_position_ids, rope_deltas = self.get_position_ids(lang_tokens, image_grid_thw, prefix_pad_masks)

        B = lang_tokens.shape[0]
        suffix_len = suffix_pad_masks.shape[1]

        if labels is not None:
            fast_mask = self._compute_fast_token_mask(lang_tokens, fast_token_mask)
            has_fast = fast_mask.any(dim=1)  # [B]
            first_fast_idx = fast_mask.long().argmax(dim=1)
            anchor_idx = (first_fast_idx - 1).clamp(min=0)

            batch_idx = torch.arange(B, device=lang_tokens.device)
            max_input_pos_before_fast = prefix_position_ids[:, batch_idx, anchor_idx].unsqueeze(-1)
            max_input_pos_all = prefix_position_ids.max(dim=-1, keepdim=True).values

            max_input_pos = torch.where(
                has_fast.view(1, -1, 1),
                max_input_pos_before_fast,
                max_input_pos_all,
            )
        else:
            max_input_pos = prefix_position_ids.max(dim=-1, keepdim=True).values

        suffix_position_ids = (
            torch.arange(1, suffix_len + 1).repeat(3, 1, 1).to(max_input_pos)
            + max_input_pos
        )
        position_ids = torch.cat([prefix_position_ids, suffix_position_ids], dim=-1)

        att_2d_masks_4d = self._prepare_attention_masks_4d(att_2d_masks)

        def forward_func(prefix_embs, suffix_embs, att_2d_masks_4d, position_ids):
            (prefix_out, suffix_out), _ = self.qwen3_5_with_expert.forward(
                attention_mask=att_2d_masks_4d,
                position_ids=position_ids,
                past_key_values=None,
                inputs_embeds=[prefix_embs, suffix_embs],
                use_cache=False,
                knowledge_insulation=self.config.knowledge_insulation,
                use_sdpa=self.config.use_sdpa,
                linear_attn_mask=pad_masks,
            )
            return prefix_out, suffix_out

        prefix_out, suffix_out = self._apply_checkpoint(
            forward_func, prefix_embs, suffix_embs, att_2d_masks_4d, position_ids
        )

        if labels is not None:
            logits = self.qwen3_5_with_expert.qwen3_5.lm_head(prefix_out).to(dtype=torch.float32)
            labels = labels.to(device=logits.device, dtype=torch.long)
            bsize, seq_len, vocab_size = logits.shape
            logits_ar = logits[:, :-1, :].reshape(-1, vocab_size)
            labels_ar = labels[:, 1:].reshape(-1)
            loss_per_token = F.cross_entropy(
                logits_ar, labels_ar, reduction="none", ignore_index=-100
            ).reshape(bsize, seq_len - 1)
            token_mask = labels[:, 1:] != -100
            valid = token_mask.float()
            valid_per_sample = valid.sum(dim=1).clamp(min=1)
            loss_vqa = (loss_per_token * valid).sum(dim=1) / valid_per_sample
        else:
            bsize = actions.shape[0]
            loss_per_token = torch.zeros(
                bsize, 1, device=actions.device, dtype=torch.float32
            )
            token_mask = torch.zeros(bsize, 1, device=actions.device, dtype=torch.bool)
            loss_vqa = torch.zeros(bsize, device=actions.device, dtype=torch.float32)

        suffix_out = suffix_out[:, -self.config.chunk_size :]
        suffix_out = suffix_out.to(dtype=torch.float32)

        def action_out_proj_func(suffix_out):
            return self.action_out_proj(suffix_out)

        v_t = self._apply_checkpoint(action_out_proj_func, suffix_out)

        loss_action = F.mse_loss(u_t, v_t, reduction="none")
        return loss_action, loss_vqa, loss_per_token, token_mask

    @torch.no_grad()
    def generate_subtask_tokens(
        self,
        pixel_values: Tensor,
        image_grid_thw: Tensor,
        lang_tokens: Tensor,
        lang_masks: Tensor,
        max_new_tokens: int,
    ) -> tuple[Tensor, Tensor]:
        generated = self.qwen3_5_with_expert.qwen3_5.generate(
            input_ids=lang_tokens,
            attention_mask=lang_masks,
            pixel_values=pixel_values,
            image_grid_thw=image_grid_thw,
            do_sample=False,
            max_new_tokens=max_new_tokens,
        )
        new_tokens = generated[:, lang_tokens.shape[1] :]
        if new_tokens.numel() == 0:
            return lang_tokens, lang_masks

        appended_masks = torch.ones_like(new_tokens, dtype=lang_masks.dtype, device=lang_masks.device)
        lang_tokens = torch.cat([lang_tokens, new_tokens], dim=1)
        lang_masks = torch.cat([lang_masks, appended_masks], dim=1)
        return lang_tokens, lang_masks

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
            actions_shape = (
                bsize,
                self.config.chunk_size,
                self.config.max_action_dim,
            )
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
            #attention_mask=prefix_pad_masks,
            position_ids=prefix_position_ids,
            past_key_values=None,
            inputs_embeds=[prefix_embs, None],
            use_cache=True,
            knowledge_insulation=self.config.knowledge_insulation,
        )

        dt = -1.0 / num_steps
        dt = torch.tensor(dt, dtype=torch.float32, device=device)

        x_t = noise
        time = torch.tensor(1.0, dtype=torch.float32, device=device)
        max_prefix_position_ids = prefix_position_ids.max(dim=-1, keepdim=True).values

        if self.config.block_action_attend_fast_tokens:
            fast_mask = self._compute_fast_token_mask(lang_tokens, fast_token_mask)
        else:
            fast_mask = None

        while time >= -dt / 2:
            expanded_time = time.expand(bsize)
            v_t = self.denoise_step(
                state,
                prefix_pad_masks,
                past_key_values,
                max_prefix_position_ids,
                x_t.to(dtype),
                expanded_time.to(dtype),
                fast_mask=fast_mask,
            )
            x_t = x_t + dt * v_t
            time += dt

        return x_t

    def denoise_step(
        self,
        state,
        prefix_pad_masks,
        past_key_values,
        max_prefix_position_ids,
        x_t,
        timestep,
        fast_mask: Tensor | None = None,
    ):
        suffix_embs, suffix_pad_masks, suffix_att_masks = self.embed_suffix(state, x_t, timestep)

        suffix_len = suffix_pad_masks.shape[1]
        batch_size = prefix_pad_masks.shape[0]
        prefix_len = prefix_pad_masks.shape[1]

        prefix_pad_2d_masks = prefix_pad_masks[:, None, :].expand(batch_size, suffix_len, prefix_len)
        suffix_att_2d_masks = make_att_2d_masks(suffix_pad_masks, suffix_att_masks)
        full_att_2d_masks = torch.cat([prefix_pad_2d_masks, suffix_att_2d_masks], dim=2)

        # Block suffix queries from attending to fast tokens at inference time too.
        if fast_mask is not None:
            mask_b = fast_mask.to(device=full_att_2d_masks.device, dtype=torch.bool)
            full_att_2d_masks[:, :, :prefix_len] &= ~mask_b[:, None, :]

        position_ids = (
            torch.arange(1, suffix_len + 1).repeat(3, 1, 1).to(max_prefix_position_ids)
            + max_prefix_position_ids
        )

        full_att_2d_masks_4d = self._prepare_attention_masks_4d(full_att_2d_masks)
        self.qwen3_5_with_expert.action_expert.config._attn_implementation = "eager"

        outputs_embeds, _ = self.qwen3_5_with_expert.forward(
            attention_mask=full_att_2d_masks_4d,
            position_ids=position_ids,
            past_key_values=past_key_values,
            inputs_embeds=[None, suffix_embs],
            use_cache=False,
            knowledge_insulation=self.config.knowledge_insulation,
        )

        suffix_out = outputs_embeds[1]
        suffix_out = suffix_out[:, -self.config.chunk_size :]
        suffix_out = suffix_out.to(dtype=torch.float32)
        return self.action_out_proj(suffix_out)


