"""CUDA graph inference extracted from infer_a2.py; see vendor/qwen35_history/PROVENANCE.md."""
import contextlib
import logging
import math
import time
import torch
import torch.nn.functional as F
from lerobot.policies.qwen3_5_context_video_ki.modeling_qwen3_5_context_video_ki import (
 CTX_TOKEN_START, CTX_VIDEO_TOKEN_END, PIXEL_VALUES_VIDEOS_KEY, VIDEO_GRID_THW_KEY,
 _flat_video_grid, _fp32_head, _inference_autocast, create_sinusoidal_pos_embedding, make_att_2d_masks)
from lerobot.utils.constants import OBS_STATE
GRAPH_TOLERANCE = 5e-2

@torch.no_grad()
def euler_reference(model, num_steps, past_key_values, attention_mask, anchor, state, noise):
    """``sample_actions``' own Euler loop after the prefix stage (the eager path and the graph's yardstick)."""
    dt = -1.0 / num_steps
    with _inference_autocast(model):
        x_t = noise.to(torch.float32)
        time_value = torch.tensor(1.0, dtype=torch.float32, device=x_t.device)
        while time_value >= -dt / 2:
            v_t = model.denoise_step(state, attention_mask, past_key_values, anchor, x_t, time_value.expand(1))
            x_t = x_t + dt * v_t
            time_value = time_value + dt
    return x_t


class _StaticKVCache:
    """The two lists ``Qwen3_5Attention``'s ``use_cache=False`` branch reads, over static buffers."""

    def __init__(self, key_cache, value_cache):
        self.key_cache = key_cache
        self.value_cache = value_cache


@contextlib.contextmanager
def _expert_attention(expert, implementation):
    """Temporarily switch the expert's attention kernel (every layer shares its config)."""
    previous = expert.config._attn_implementation
    expert.config._attn_implementation = implementation
    try:
        yield
    finally:
        expert.config._attn_implementation = previous


class FlowSampler:
    """The expert's Euler loop against a prefilled cache, optionally as one CUDA graph.

    Mirrors ``Qwen3_5ContextVideoKIModel.sample_actions`` after the prefix
    stage, with three changes that make it capturable:

    * the prefix K/V of the full-attention layers are copied, left-padded, into
      ``[1, kv_heads, max_prefix_len, head_dim]`` buffers; padded keys get the
      additive mask value, so they carry exactly zero attention weight and one
      capture serves every prefix length;
    * the expert layers are called directly instead of through
      ``Qwen3_5TextModel.forward``, whose linear-attention mask helper branches
      on a GPU tensor (a host sync, illegal while capturing).  Linear-attention
      layers get no cache, as in the training joint pass -- which also stops
      the expert overwriting the VLM cache's recurrent states;
    * the suffix block mask and the Euler times are precomputed, so the graph
      holds no host-to-device copies.

    ``reference`` runs the model's own ``denoise_step`` loop on the real cache;
    warmup compares the two on the same noise.
    """

    def __init__(self, model, *, num_steps, max_prefix_len, attn_implementation="sdpa"):
        self.model = model
        self.expert = model.action_expert
        self.num_steps = num_steps
        self.max_prefix_len = max_prefix_len
        self.attn_implementation = attn_implementation
        config = model.config
        device = model.action_out_proj.weight.device
        expert_dtype = next(self.expert.parameters()).dtype
        layers = self.expert.layers
        self.full_layers = [index for index, layer in enumerate(layers) if layer.layer_type == "full_attention"]
        attention = layers[self.full_layers[0]].self_attn
        kv_heads, head_dim = attention.config.num_key_value_heads, attention.head_dim
        key_cache, value_cache = [None] * len(layers), [None] * len(layers)
        for index in self.full_layers:
            key_cache[index] = torch.zeros((1, kv_heads, max_prefix_len, head_dim), dtype=expert_dtype, device=device)
            value_cache[index] = torch.zeros_like(key_cache[index])
        self.cache = _StaticKVCache(key_cache, value_cache)
        self.prefix_mask = torch.zeros((1, max_prefix_len), dtype=torch.bool, device=device)
        self.anchor = torch.zeros((4, 1, 1), dtype=torch.long, device=device)
        self.state = torch.zeros((1, config.max_state_dim), dtype=torch.float32, device=device)
        self.noise = torch.zeros((1, config.chunk_size, config.max_action_dim), dtype=torch.float32, device=device)
        self.output = torch.zeros_like(self.noise)

        suffix_len = config.chunk_size + 1
        self.suffix_offsets = torch.arange(1, suffix_len + 1, device=device)
        block = torch.tensor([1, 1] + [0] * (config.chunk_size - 1), dtype=torch.long, device=device)
        self.suffix_2d = make_att_2d_masks(torch.ones((1, suffix_len), dtype=torch.bool, device=device), block[None])
        # The same float32 time sequence ``sample_actions`` walks, as Python floats.
        dt = -1.0 / num_steps
        self.dt = dt
        self.times = []
        time_value = torch.tensor(1.0, dtype=torch.float32)
        while time_value >= -dt / 2:
            self.times.append(float(time_value))
            time_value = time_value + dt
        self.graph = None

    def load(self, past_key_values, attention_mask, anchor, state):
        """Copy one prefix stage's outputs into the static buffers; False if it does not fit."""
        prefix_len = attention_mask.shape[1]
        if attention_mask.shape[0] != 1 or prefix_len > self.max_prefix_len:
            return False
        start = self.max_prefix_len - prefix_len
        for index in self.full_layers:
            for buffer, source in ((self.cache.key_cache[index], past_key_values.key_cache[index]),
                                   (self.cache.value_cache[index], past_key_values.value_cache[index])):
                buffer[:, :, :start].zero_()
                buffer[:, :, start:].copy_(source)
        self.prefix_mask[:, :start] = False
        self.prefix_mask[:, start:].copy_(attention_mask)
        self.anchor.copy_(anchor)
        self.state.copy_(state.to(torch.float32))
        return True

    def _velocity(self, x_t, timestep, attention_mask, position_embeddings):
        model = self.model
        # ``embed_suffix`` without its per-call mask tensor.
        state_emb = model.state_proj(self.state.to(model.state_proj.weight.dtype))
        time_emb = create_sinusoidal_pos_embedding(
            timestep, model.action_in_proj.out_features, min_period=model.config.min_period,
            max_period=model.config.max_period, device=timestep.device,
        ).to(model.action_in_proj.weight.dtype)
        action_emb = model.action_in_proj(x_t.to(model.action_in_proj.weight.dtype))
        action_time_emb = torch.cat((action_emb, time_emb[:, None, :].expand_as(action_emb)), dim=2)
        action_time_emb = model.action_time_mlp_out(F.silu(model.action_time_mlp_in(action_time_emb)))
        hidden = torch.cat((state_emb[:, None, :], action_time_emb), dim=1)
        hidden = hidden.to(dtype=model.qwen.get_input_embeddings().weight.dtype)
        for layer in self.expert.layers:
            if layer.layer_type == "linear_attention":
                hidden = layer(hidden, position_embeddings=position_embeddings, attention_mask=None)
            else:
                hidden = layer(
                    hidden, position_embeddings=position_embeddings, attention_mask=attention_mask,
                    past_key_values=self.cache, use_cache=False,
                )
        hidden = self.expert.norm(hidden)
        return _fp32_head(model.action_out_proj, hidden[:, -model.config.chunk_size :])

    def _run(self):
        """The whole Euler loop over the static buffers (the captured body)."""
        suffix_len = self.suffix_2d.shape[1]
        prefix_2d = self.prefix_mask[:, None, :].expand(1, suffix_len, self.max_prefix_len)
        attention_mask = self.model._additive_mask(torch.cat((prefix_2d, self.suffix_2d), dim=2))
        if self.attn_implementation != "eager":
            # SDPA wants the mask in the query dtype (the joint training pass casts it the same way).
            attention_mask = attention_mask.to(next(self.expert.parameters()).dtype)
        position_ids = self.anchor + self.suffix_offsets
        hidden_like = torch.empty((1, suffix_len, 1), dtype=next(self.expert.parameters()).dtype,
                                  device=position_ids.device)
        position_embeddings = self.expert.rotary_emb(hidden_like, position_ids[1:])
        x_t = self.noise
        for time_value in self.times:
            timestep = torch.full((1,), time_value, dtype=torch.float32, device=x_t.device)
            x_t = x_t + self.dt * self._velocity(x_t, timestep, attention_mask, position_embeddings)
        self.output.copy_(x_t)

    def _autocast(self):
        # Weight-cast caching must be off while capturing under autocast.
        return torch.autocast(device_type="cuda", dtype=torch.bfloat16, cache_enabled=False)

    @torch.no_grad()
    def capture(self):
        stream = torch.cuda.Stream()
        stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(stream), self._autocast(), _expert_attention(self.expert, self.attn_implementation):
            for _ in range(2):  # Triton autotuning and lazy init happen outside the graph
                self._run()
        torch.cuda.current_stream().wait_stream(stream)
        graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(graph), self._autocast(), _expert_attention(self.expert, self.attn_implementation):
            self._run()
        self.graph = graph

    @torch.no_grad()
    def replay(self):
        self.graph.replay()
        return self.output.clone()

    def reference(self, past_key_values, attention_mask, anchor, state, noise):
        return euler_reference(self.model, self.num_steps, past_key_values, attention_mask, anchor, state, noise)

    def sample(self, past_key_values, attention_mask, anchor, state):
        """One normalized ``[1, chunk_size, max_action_dim]`` chunk, through the fastest valid path."""
        if self.graph is not None and self.load(past_key_values, attention_mask, anchor, state):
            self.noise.normal_()
            return self.replay()
        if self.graph is not None:
            logging.warning("prefix of %d tokens exceeds the graph's %d; eager denoise for this replan",
                            attention_mask.shape[1], self.max_prefix_len)
        noise = self.model.sample_noise(tuple(self.noise.shape), self.noise.device)
        return self.reference(past_key_values, attention_mask, anchor, state.to(torch.float32), noise)


class _KVWriter:
    """The ``past_key_values.update`` the full-attention layers call during prefill, into static buffers."""

    def __init__(self, key_cache, value_cache):
        self.key_cache = key_cache
        self.value_cache = value_cache

    def update(self, key_states, value_states, layer_idx, cache_kwargs=None):
        self.key_cache[layer_idx].copy_(key_states)
        self.value_cache[layer_idx].copy_(value_states)
        return key_states, value_states


class PrefixGraph:
    """The action-first prefix stage (ViT + LM prefill) as CUDA graphs that write a FlowSampler's K/V.

    Mirrors ``_prefix_stage`` -> ``_prefill_generation`` -> ``embed_inputs`` for
    one row, with everything that host-syncs moved out of the graph:

    * the semantic-prefix cut, the marker / vision / state placeholder positions
      and ``get_rope_index`` run on CPU tensors in ``prepare``; ``load`` copies
      them into static buffers (positions move as the clip timestamps grow);
    * the ViT's per-grid constants (interpolated position embeddings, rotary
      cos/sin, the per-slice attention lengths) are computed once per grid;
    * placeholders are filled with ``index_copy`` instead of boolean indexing,
      and the language layers are called directly, as in ``FlowSampler``.

    The prompt is RIGHT-padded to the sampler's ``max_prefix_len``: every layer
    is causal (full attention, causal conv1d, the delta rule), so tokens after
    the prompt cannot reach it, and the sampler's prefix mask hides their keys.
    Left padding would feed the pad embeddings into the linear layers' state.

    One graph per vision layout (current-view grids, clip grid, marker count):
    the history ramp walks through a handful, full history is one.  The graphs
    share a memory pool; their inputs and outputs live outside it.
    """

    def __init__(self, model, sampler):
        self.model = model
        self.sampler = sampler
        self.language = model.qwen.model.language_model
        self.visual = model.qwen.model.visual
        self.max_prefix_len = sampler.max_prefix_len
        self.layers = list(self.language.layers[: self.language.config.num_hidden_layers])
        full_layers = [index for index, layer in enumerate(self.layers) if layer.layer_type == "full_attention"]
        if full_layers != sampler.full_layers:
            raise ValueError(f"VLM full-attention layers {full_layers} differ from the expert's {sampler.full_layers}")
        if self.visual.config._attn_implementation not in ("sdpa", "eager"):
            raise ValueError("the graphed ViT splits attention per slice; it needs the sdpa or eager kernel")
        self.writer = _KVWriter(sampler.cache.key_cache, sampler.cache.value_cache)
        device = sampler.prefix_mask.device
        self.ids = torch.zeros((1, self.max_prefix_len), dtype=torch.long, device=device)
        self.positions = torch.zeros((4, 1, self.max_prefix_len), dtype=torch.long, device=device)
        self.graphs = {}
        self.pool = None

    def prepare(self, model_input):
        """CPU half of one replan: the row's prefix ids, positions, anchor and placeholder slots."""
        model = self.model
        input_ids = torch.as_tensor(model_input["observation.input_ids"]).long()[None]
        attention_mask = torch.as_tensor(model_input["observation.attention_mask"]).bool()[None]
        ids = torch.tensor(model._extract_semantic_prefix(
            input_ids, attention_mask, 0, selected_mode_token=model.special_tokens.action_image
        ))
        marker = (ids >= CTX_TOKEN_START) & (ids < CTX_VIDEO_TOKEN_END)
        safe_ids = ids.masked_fill(marker, 0)
        image_grid = torch.as_tensor(model_input["observation.image_grid_thw"]).long().reshape(-1, 3)
        video_grid = _flat_video_grid(model_input.get(VIDEO_GRID_THW_KEY))
        full_mask = torch.ones((1, len(ids)), dtype=torch.bool)
        positions, rope_deltas = model._build_position_ids(
            safe_ids[None], image_grid, full_mask, video_grid_thw=video_grid
        )
        merge = self.visual.spatial_merge_size ** 2
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
        prefix_len = len(ids)
        text_anchor = torch.full((1, 1, 1), prefix_len - 1, dtype=torch.long)
        vision_anchor = (prefix_len - 1 + rope_deltas[:, 0]).view(1, 1, 1).expand(3, 1, 1)
        pixels = torch.as_tensor(model_input["observation.pixel_values"])
        videos = model_input.get(PIXEL_VALUES_VIDEOS_KEY) if video_grid is not None else None
        return {
            "key": (tuple(map(tuple, image_grid.tolist())),
                    () if video_grid is None else tuple(map(tuple, video_grid.tolist())), len(slots["marker"])),
            "prefix_len": prefix_len, "ids": safe_ids, "positions": positions, "slots": slots,
            "marker_index": ids[marker] - CTX_TOKEN_START,
            "anchor": torch.cat((text_anchor, vision_anchor), dim=0),
            "image_grid": image_grid, "video_grid": video_grid,
            "pixels": pixels.reshape(-1, pixels.shape[-1]),
            "videos": None if videos is None else torch.as_tensor(videos).reshape(-1, pixels.shape[-1]),
            # make_batch rounds the state through bf16 before the projection; so does this.
            "state": torch.as_tensor(model_input[OBS_STATE]).to(torch.bfloat16).float()[None],
        }

    def _vision_constants(self, grid):
        """What ``Qwen3_5VisionModel.forward`` derives from a fixed grid, computed once."""
        visual = self.visual
        device = self.ids.device
        grid_device = grid.to(device)
        pos_embeds = visual.fast_pos_embed_interpolate(grid_device)
        rotary = visual.rot_pos_emb(grid_device).reshape(pos_embeds.shape[0], -1)
        emb = torch.cat((rotary, rotary), dim=-1)
        # The SDPA branch of Qwen3_5VisionAttention only reads per-slice lengths
        # off cu_seqlens; a CPU copy keeps its ``.tolist()`` off the device.
        cu_seqlens = F.pad(torch.repeat_interleave(grid[:, 1] * grid[:, 2], grid[:, 0]).cumsum(0, dtype=torch.int32),
                           (1, 0))
        return {"pos_embeds": pos_embeds, "position_embeddings": (emb.cos(), emb.sin()), "cu_seqlens": cu_seqlens}

    def _vision(self, pixels, constants):
        visual = self.visual
        hidden = visual.patch_embed(pixels.type(visual.dtype)) + constants["pos_embeds"]
        for block in visual.blocks:
            hidden = block(hidden, cu_seqlens=constants["cu_seqlens"],
                           position_embeddings=constants["position_embeddings"])
        return visual.merger(hidden)

    def _run(self, entry):
        """Embed, fill the placeholders, prefill the language layers (the captured body)."""
        model = self.model
        embeds = model.qwen.get_input_embeddings()(self.ids)[0]
        slots = entry["slots"]
        if "marker" in slots:
            markers = model.context_marker_embeddings(entry["marker_index"])
            embeds = embeds.index_copy(0, slots["marker"], markers.to(embeds.dtype))
        embeds = embeds.index_copy(0, slots["image"], self._vision(entry["pixels"], entry["image"]).to(embeds.dtype))
        if "video" in slots:
            videos = self._vision(entry["videos"], entry["video"])
            embeds = embeds.index_copy(0, slots["video"], videos.to(embeds.dtype))
        state = model.state_projection(self.sampler.state.to(model.state_projection.weight.dtype))
        embeds = embeds.index_copy(0, slots["state"], state.to(embeds.dtype))
        hidden = embeds[None]
        position_embeddings = self.language.rotary_emb(hidden, self.positions[1:])
        for layer in self.layers:
            if layer.layer_type == "linear_attention":
                hidden = layer(hidden, position_embeddings=position_embeddings, attention_mask=None)
            else:
                # Causal SDPA over the padded row: real tokens never see the pads.
                hidden = layer(hidden, position_embeddings=position_embeddings, attention_mask=None,
                               past_key_values=self.writer, use_cache=True)

    def load(self, prepared):
        """Copy a prepared row into the static buffers; the graph entry, or None if no graph fits."""
        entry = self.graphs.get(prepared["key"])
        prefix_len = prepared["prefix_len"]
        if entry is None or prefix_len > self.max_prefix_len:
            return None
        ids = torch.zeros((1, self.max_prefix_len), dtype=torch.long)
        ids[0, :prefix_len] = prepared["ids"]
        positions = torch.zeros((4, 1, self.max_prefix_len), dtype=torch.long)
        positions[:, :, :prefix_len] = prepared["positions"]
        self.ids.copy_(ids)
        self.positions.copy_(positions)
        for name, index in prepared["slots"].items():
            if name in entry["slots"]:
                entry["slots"][name].copy_(index)
        if "marker" in entry["slots"]:
            entry["marker_index"].copy_(prepared["marker_index"])
        entry["pixels"].copy_(prepared["pixels"])
        if entry["videos"] is not None:
            entry["videos"].copy_(prepared["videos"])
        sampler = self.sampler
        sampler.prefix_mask.zero_()
        sampler.prefix_mask[:, :prefix_len] = True
        sampler.anchor.copy_(prepared["anchor"])
        sampler.state.copy_(prepared["state"])
        return entry

    def _autocast(self):
        # The prefix stage runs under _inference_autocast; caching is off while capturing.
        return torch.autocast(device_type="cuda", dtype=torch.bfloat16, cache_enabled=False)

    @torch.no_grad()
    def capture(self, prepared):
        """Allocate one layout's buffers, warm it up on a side stream and capture it."""
        device = self.ids.device
        vision_dtype = self.visual.dtype
        # Empty slot kinds (no markers / no clip) stay out of the body entirely.
        slots = {name: torch.zeros_like(index, device=device) for name, index in prepared["slots"].items()
                 if index.numel()}
        with self._autocast():
            entry = {
                "slots": slots,
                "marker_index": torch.zeros_like(prepared["marker_index"], device=device),
                "pixels": torch.zeros(prepared["pixels"].shape, dtype=vision_dtype, device=device),
                "videos": None if "video" not in slots else torch.zeros(
                    prepared["videos"].shape, dtype=vision_dtype, device=device),
                "image": self._vision_constants(prepared["image_grid"]),
                "video": None if "video" not in slots else self._vision_constants(prepared["video_grid"]),
            }
        self.graphs[prepared["key"]] = entry
        self.load(prepared)
        stream = torch.cuda.Stream()
        stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(stream), self._autocast():
            for _ in range(2):  # Triton autotuning and lazy init happen outside the graph
                self._run(entry)
        torch.cuda.current_stream().wait_stream(stream)
        if self.pool is None:
            self.pool = torch.cuda.graph_pool_handle()
        graph = torch.cuda.CUDAGraph()
        try:
            with torch.cuda.graph(graph, pool=self.pool), self._autocast():
                self._run(entry)
        except Exception:
            del self.graphs[prepared["key"]]
            raise
        entry["graph"] = graph

    def drop(self, key):
        self.graphs.pop(key, None)

    def run(self, prepared):
        """Replay the layout's graph into the sampler's buffers; False when the eager stage must run."""
        entry = self.load(prepared)
        if entry is None or "graph" not in entry:
            return False
        entry["graph"].replay()
        return True


def capture_prefix_graphs(model, sampler, model_inputs, *, eager_prefix):
    """One ``PrefixGraph`` layout per distinct vision layout in ``model_inputs``, each checked against the model.

    The check runs the eager ``_prefix_stage`` and the graph on the same row and
    compares the K/V they hand the sampler and the denoise-graph chunk on one
    noise draw; a layout outside ``GRAPH_TOLERANCE`` is dropped and stays eager.
    Returns None when no layout survives.
    """
    prefix_graph = PrefixGraph(model, sampler)
    seen = set()
    timed_input = None
    for model_input in model_inputs:
        prepared = prefix_graph.prepare(model_input)
        key, prefix_len = prepared["key"], prepared["prefix_len"]
        if key in seen:
            continue
        seen.add(key)
        if prefix_len > sampler.max_prefix_len:
            logging.warning("prefix of %d tokens exceeds the %d-token buffers; layout stays eager",
                            prefix_len, sampler.max_prefix_len)
            continue
        try:
            prefix_graph.capture(prepared)
            past_key_values, attention_mask, anchor, state = eager_prefix(model_input)
            if attention_mask.shape[1] != prefix_len or not torch.equal(anchor.cpu(), prepared["anchor"]):
                raise RuntimeError("prepared prefix length / anchor disagree with _prefix_stage")
            noise = torch.randn_like(sampler.noise)
            sampler.load(past_key_values, attention_mask, anchor, state)
            sampler.noise.copy_(noise)
            eager_chunk = sampler.replay()
            prefix_graph.run(prepared)
            kv_diff = 0.0
            for index in sampler.full_layers:
                for buffer, source in ((sampler.cache.key_cache[index], past_key_values.key_cache[index]),
                                       (sampler.cache.value_cache[index], past_key_values.value_cache[index])):
                    error = (buffer[:, :, :prefix_len].float() - source.float()).abs().max()
                    kv_diff = max(kv_diff, float(error / source.float().abs().max()))
            sampler.noise.copy_(noise)
            diff = float((sampler.replay() - eager_chunk).abs().max())
        except Exception:  # noqa: BLE001 -- capture support varies with the kernels installed
            logging.exception("prefix graph capture failed for layout %s; it stays eager", key)
            prefix_graph.drop(key)
            continue
        logging.info("prefix graph %s: %d tokens | K/V max rel diff %.2e | chunk max |diff| %.2e (tolerance %.0e)",
                     key[1] or "no clip", prefix_len, kv_diff, diff, GRAPH_TOLERANCE)
        if not math.isfinite(diff) or diff > GRAPH_TOLERANCE:
            logging.warning("prefix graph disagrees with _prefix_stage for layout %s; it stays eager", key)
            prefix_graph.drop(key)
            continue
        timed_input = model_input
    if not prefix_graph.graphs:
        logging.info("prefix path: model's own eager _prefix_stage")
        return None
    # Timing on the last surviving layout (the full history when it is graphed).
    timings = {}
    for name, run in (("graph", lambda: prefix_graph.run(prefix_graph.prepare(timed_input))),
                      ("eager", lambda: eager_prefix(timed_input))):
        run()
        torch.cuda.synchronize()
        start = time.perf_counter()
        for _ in range(5):
            run()
        torch.cuda.synchronize()
        timings[name] = (time.perf_counter() - start) / 5 * 1e3
    logging.info("prefix graph vs _prefix_stage: %d layouts graphed | graph %.1f ms (incl. CPU prepare), eager %.1f ms",
                 len(prefix_graph.graphs), timings["graph"], timings["eager"])
    return prefix_graph


