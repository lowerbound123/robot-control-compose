"""Masked GQA using the regular SDPA backend selection."""

from __future__ import annotations

from torch import Tensor
from torch.nn import functional


def masked_gqa_sdpa(
    query: Tensor,
    key: Tensor,
    value: Tensor,
    attention_mask: Tensor,
    *,
    scale: float | None = None,
) -> Tensor:
    """Preserve masked GQA while allowing the memory-efficient CUDA backend.

    Native ``enable_gqa=True`` restricts the available SDPA backends. Expanding
    the KV heads explicitly permits the regular masked attention dispatch.
    Only the mask's storage is padded: its logical shape and values stay the
    same. Aligned row strides satisfy the efficient CUDA kernel's bias layout.
    No weights, activations, or gradients are cast to a different dtype.
    """
    query_heads, key_heads, value_heads = query.shape[-3], key.shape[-3], value.shape[-3]
    if min(query_heads, key_heads, value_heads) < 1 or key_heads != value_heads or query_heads % key_heads:
        raise ValueError("GQA requires equal KV head counts dividing the query head count")
    repeats = query_heads // key_heads
    if repeats > 1:
        key = key.repeat_interleave(repeats, dim=-3)
        value = value.repeat_interleave(repeats, dim=-3)

    key_length = attention_mask.shape[-1]
    padding = (-key_length) % 8
    if padding:
        attention_mask = functional.pad(attention_mask, (0, padding))[..., :key_length]
    elif not attention_mask.is_contiguous():
        attention_mask = attention_mask.contiguous()

    return functional.scaled_dot_product_attention(
        query,
        key,
        value,
        attn_mask=attention_mask,
        scale=scale,
        enable_gqa=False,
    )
