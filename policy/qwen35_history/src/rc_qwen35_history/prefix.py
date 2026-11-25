"""Multimodal prefix geometry shared by eager A2 inference and prefix graphs."""
import torch
import torch.nn.functional as F

from lerobot.policies.qwen3_5_context_video.tokenization_qwen3_5_context_video import (
    CTX_TOKEN_START, CTX_VIDEO_TOKEN_END,
)


def semantic_prompt(model, model_input):
    ids = torch.as_tensor(model_input["observation.input_ids"]).long()
    mask = torch.as_tensor(model_input["observation.attention_mask"]).bool()
    if ids.ndim != 1 or mask.shape != ids.shape:
        raise ValueError("expected one processed prompt and a matching attention mask")
    return model._extract_semantic_prefix(ids[None], mask[None], 0, selected_mode_token=model.special_tokens.action_image)


def prompt_layout(model, tokens, image_grid, video_grid):
    """Prepare current text, timestamp positions and embedding slots on the CPU."""
    ids = torch.tensor(tokens, dtype=torch.long)
    marker = (ids >= CTX_TOKEN_START) & (ids < CTX_VIDEO_TOKEN_END)
    if bool(((ids < 0) | ((ids >= model.vocab_size) & ~marker)).any()):
        raise ValueError("prefix contains a token outside the model vocabulary")
    safe_ids = ids.masked_fill(marker, 0)
    full_mask = torch.ones((1, len(ids)), dtype=torch.bool)
    positions, rope_deltas = model._build_position_ids(
        safe_ids[None], image_grid, full_mask, video_grid_thw=video_grid
    )
    merge = model.qwen.model.visual.spatial_merge_size ** 2
    slots = {
        "image": (safe_ids == int(model.qwen.config.image_token_id)).nonzero().flatten(),
        "video": (safe_ids == int(model.qwen.config.video_token_id)).nonzero().flatten(),
        "state": (safe_ids == model.special_tokens.learned_state).nonzero().flatten(),
        "marker": marker.nonzero().flatten(),
    }
    expected_video = 0 if video_grid is None else int(video_grid.prod(-1).sum()) // merge
    if (len(slots["image"]) != int(image_grid.prod(-1).sum()) // merge or len(slots["video"]) != expected_video
            or len(slots["state"]) != 1):
        raise ValueError("prefix placeholders do not match the vision grids / the single state token")
    return {"ids": safe_ids, "positions": positions, "deltas": rope_deltas, "mask": full_mask,
            "slots": slots, "marker_index": ids[marker] - CTX_TOKEN_START}


def vision_constants(visual, grid):
    """What ``Qwen3_5VisionModel.forward`` derives from a fixed grid, computed once."""
    pos_embeds = visual.fast_pos_embed_interpolate(grid)
    rotary = visual.rot_pos_emb(grid).reshape(pos_embeds.shape[0], -1)
    emb = torch.cat((rotary, rotary), dim=-1)
    # The SDPA branch of Qwen3_5VisionAttention only needs per-slice lengths.
    # Derive them directly from the processor's CPU grid.
    cu_seqlens = F.pad(torch.repeat_interleave(grid[:, 1] * grid[:, 2], grid[:, 0]).cumsum(0, dtype=torch.int32),
                       (1, 0))
    return {"pos_embeds": pos_embeds, "position_embeddings": (emb.cos(), emb.sin()),
            "cu_seqlens": cu_seqlens, "sdpa_slice_lengths": visual.sdpa_slice_lengths(cu_seqlens)}


def encode_vision(visual, pixels, constants):
    hidden = visual.patch_embed(pixels.type(visual.dtype)) + constants["pos_embeds"]
    for block in visual.blocks:
        hidden = block(hidden, cu_seqlens=constants["cu_seqlens"],
                       sdpa_slice_lengths=constants["sdpa_slice_lengths"],
                       position_embeddings=constants["position_embeddings"])
    return visual.merger(hidden)
