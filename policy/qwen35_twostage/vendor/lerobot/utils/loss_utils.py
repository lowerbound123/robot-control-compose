# Adapted for inference-only robot-control deployment; see PROVENANCE.md.
from __future__ import annotations
from typing import Any
import torch
from torch import Tensor

def masked_mean(values: Tensor, mask: Tensor) -> Tensor:
    """Return a scalar masked mean, or a graph-connected zero for an empty mask."""
    if values.shape != mask.shape:
        raise ValueError(
            f"values and mask must have the same shape, got {values.shape} and {mask.shape}"
        )
    mask = mask.to(device=values.device, dtype=values.dtype)
    return (values * mask).sum() / mask.sum().clamp(min=1)


def split_vlm_token_loss(
    loss_per_token: Tensor,
    token_mask: Tensor,
    labels: Tensor,
    action_token_min: int,
    action_token_max: int,
) -> tuple[Tensor, Tensor]:
    """Split valid next-token CE into FAST-action and non-FAST text losses."""
    shifted_labels = labels[:, 1:].to(device=loss_per_token.device)
    token_mask = token_mask.to(device=loss_per_token.device, dtype=torch.bool)
    if shifted_labels.shape != loss_per_token.shape or token_mask.shape != loss_per_token.shape:
        raise ValueError(
            "labels[:, 1:], loss_per_token, and token_mask must have matching shapes, "
            f"got {shifted_labels.shape}, {loss_per_token.shape}, and {token_mask.shape}"
        )

    fast_mask = (
        token_mask
        & (shifted_labels >= action_token_min)
        & (shifted_labels <= action_token_max)
    )
    subtask_mask = token_mask & ~fast_mask
    return (
        masked_mean(loss_per_token, fast_mask),
        masked_mean(loss_per_token, subtask_mask),
    )


def build_loss_metrics(
    loss: Tensor,
    *,
    loss_action: Tensor | None = None,
    loss_vqa: Tensor | None = None,
    loss_fast: Tensor | None = None,
    loss_subtask: Tensor | None = None,
    loss_video: Tensor | None = None,
) -> dict[str, float]:
    """Detach scalar training losses into the policy logging contract."""
    components = {
        "loss": loss,
        "loss_action": loss_action,
        "loss_vqa": loss_vqa,
        "loss_fast": loss_fast,
        "loss_subtask": loss_subtask,
        "loss_video": loss_video,
    }
    metrics = {}
    for name, value in components.items():
        if value is None:
            continue
        if value.numel() != 1:
            raise ValueError(f"Loss metric {name!r} must be scalar, got shape {value.shape}")
        metrics[name] = value.detach().item()
    return metrics

