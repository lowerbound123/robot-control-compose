from __future__ import annotations

import torch
from torch import nn


ACTION_TOKEN_MIN = 248077
ACTION_TOKEN_MAX = 250124
NUM_ACTION_TOKENS = ACTION_TOKEN_MAX - ACTION_TOKEN_MIN + 1


def get_action_tokens() -> list[str]:
    return [f"<robot_action_{index}>" for index in range(NUM_ACTION_TOKENS)]


def _validate_action_token_ids(tokenizer) -> None:
    token_ids = [tokenizer.convert_tokens_to_ids(token) for token in get_action_tokens()]
    expected_ids = list(range(ACTION_TOKEN_MIN, ACTION_TOKEN_MAX + 1))
    if token_ids != expected_ids:
        integer_ids = [token_id for token_id in token_ids if isinstance(token_id, int)]
        actual = (
            f"[{min(integer_ids)}, {max(integer_ids)}]" if integer_ids else "no valid ids"
        )
        raise ValueError(
            "Qwen3.5 action token ids mismatch: expected "
            f"[{ACTION_TOKEN_MIN}, {ACTION_TOKEN_MAX}], got {actual}."
        )


def _initialize_token_rows(weight: torch.Tensor, start: int, end: int) -> None:
    """Initialize newly activated token rows from the pretrained vocabulary mean."""
    start = max(start, 0)
    end = min(end, weight.shape[0])
    if start >= end:
        return
    with torch.no_grad():
        reference = weight[:ACTION_TOKEN_MIN].mean(dim=0, keepdim=True)
        weight[start:end].copy_(reference.expand(end - start, -1))


def _ensure_lm_head_size(model: nn.Module, target_size: int) -> nn.Module | None:
    get_output_embeddings = getattr(model, "get_output_embeddings", None)
    output_embeddings = get_output_embeddings() if get_output_embeddings is not None else None
    if output_embeddings is None or output_embeddings.weight.shape[0] >= target_size:
        return output_embeddings

    old_head = output_embeddings
    new_head = nn.Linear(
        old_head.in_features,
        target_size,
        bias=old_head.bias is not None,
        device=old_head.weight.device,
        dtype=old_head.weight.dtype,
    )
    with torch.no_grad():
        old_size = old_head.weight.shape[0]
        new_head.weight[:old_size].copy_(old_head.weight)
        if old_head.bias is not None:
            new_head.bias[:old_size].copy_(old_head.bias)
            new_head.bias[old_size:].zero_()

    set_output_embeddings = getattr(model, "set_output_embeddings", None)
    if set_output_embeddings is not None:
        set_output_embeddings(new_head)
    elif hasattr(model, "lm_head"):
        model.lm_head = new_head
    else:
        raise ValueError("Qwen3.5 model does not expose a resizable output embedding layer")
    return new_head


def ensure_qwen35_action_tokens(tokenizer, model: nn.Module | None = None) -> None:
    """Add FAST action tokens and, when supplied, resize the Qwen3.5 model.

    The fixed ids intentionally start at the end of the base Qwen3.5 tokenizer
    vocabulary. Model vocabularies can be larger because Hugging Face pads the
    embedding table, so the padding width is preserved while resizing.
    """
    action_tokens = get_action_tokens()
    vocabulary = tokenizer.get_vocab()
    present = [token in vocabulary for token in action_tokens]
    if any(present) and not all(present):
        raise ValueError("Qwen3.5 tokenizer has a partial set of robot action tokens")

    if not any(present):
        tokenizer.add_special_tokens({"additional_special_tokens": action_tokens})
    _validate_action_token_ids(tokenizer)

    if model is None:
        return

    input_embeddings = model.get_input_embeddings()
    old_size = input_embeddings.weight.shape[0]
    had_action_capacity = old_size > ACTION_TOKEN_MAX

    target_size = max(old_size, len(tokenizer), ACTION_TOKEN_MAX + 1)
    if not had_action_capacity and old_size > ACTION_TOKEN_MIN:
        padding_rows = old_size - ACTION_TOKEN_MIN
        target_size = max(target_size, len(tokenizer) + padding_rows)

    if old_size < target_size:
        model.resize_token_embeddings(target_size)

    output_embeddings = _ensure_lm_head_size(model, target_size)
    tie_weights = getattr(model, "tie_weights", None)
    if tie_weights is not None:
        tie_weights()

    input_embeddings = model.get_input_embeddings()
    get_output_embeddings = getattr(model, "get_output_embeddings", None)
    if get_output_embeddings is not None:
        output_embeddings = get_output_embeddings()

    if input_embeddings.weight.shape[0] < target_size:
        raise ValueError("Qwen3.5 input embeddings were not resized to match the tokenizer")
    if output_embeddings is not None and output_embeddings.weight.shape[0] < target_size:
        raise ValueError("Qwen3.5 lm_head was not resized to match the tokenizer")

    if not had_action_capacity:
        _initialize_token_rows(
            input_embeddings.weight,
            ACTION_TOKEN_MIN,
            ACTION_TOKEN_MAX + 1,
        )
        if (
            output_embeddings is not None
            and output_embeddings.weight.data_ptr() != input_embeddings.weight.data_ptr()
        ):
            _initialize_token_rows(
                output_embeddings.weight,
                ACTION_TOKEN_MIN,
                ACTION_TOKEN_MAX + 1,
            )

    if hasattr(model.config, "text_config"):
        model.config.text_config.vocab_size = input_embeddings.weight.shape[0]
    model.config.vocab_size = input_embeddings.weight.shape[0]
