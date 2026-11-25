"""History actions conditioned on the latest complete language segment."""
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
import time

import torch

from rc_qwen35_history.backend import HistoryBackend
from rc_qwen35_history.prefix import encode_vision, prompt_layout, semantic_prompt, vision_constants

PREFIX_STAGES = ('action_camera_ms', 'action_history_video_ms', 'action_lm_prefill_ms')
EMPTY_SUBTASK_TIMINGS = dict(subtask_ms=0., subtask_queue_ms=0., subtask_prepare_ms=0.,
                             subtask_prefill_ms=0., subtask_decode_ms=0., subtask_post_ms=0.)


@contextmanager
def _timed_slice(cuda, samples):
    if cuda:
        start, end = (torch.cuda.Event(enable_timing=True) for _ in range(2))
        start.record()
        yield
        end.record()
        samples.append((start, end))
    else:
        start = time.perf_counter()
        yield
        samples.append((time.perf_counter() - start) * 1000)


@dataclass
class PrefixObservation:
    state: torch.Tensor
    image_grid: torch.Tensor
    video_grid: torch.Tensor | None
    images: torch.Tensor
    video: torch.Tensor | None


class OptimizedBackend(HistoryBackend):
    capture_refresh = False

    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        self.language_ids = ()
        self.subtask = ''
        self._cold_observation = self._refresh = None
        self._vision_layouts = {}
        self._stage_samples = None
        self._monitor_update(**EMPTY_SUBTASK_TIMINGS)

    def _load_policy(self, policy_type, config):
        from .checkpoint import load_single_checkpoint
        loaded = load_single_checkpoint(self.checkpoint, policy_type, config, self.device)
        if loaded is not None:
            self._monitor_update(checkpoint_load_path='single_checkpoint')
            return loaded
        self._monitor_update(checkpoint_load_path='legacy')
        return super()._load_policy(policy_type, config)

    def load(self):
        self._vision_layouts.clear()
        super().load()
        self.processor.batch_uint8_history = True
        self.model.qwen.model.visual.batch_equal_slices = True

    def build_input(self, qpos, images, instruction, contexts):
        observation = (qpos, images, instruction, contexts)
        # With an empty cache the action prompt must announce only actions.
        self._cold_observation = observation if self.capture_refresh and not self.language_ids else None
        return super().build_input(*observation, generate=self.generate_language if self.language_ids else ())

    def _time_stage(self, name):
        if self._stage_samples is None:
            return nullcontext()
        return _timed_slice(self.device.type == 'cuda', self._stage_samples[name])

    def predict(self, *args, **kwargs):
        samples = {key: [] for key in PREFIX_STAGES}
        self._stage_samples = samples
        try:
            result, timings = super().predict(*args, **kwargs)
        finally:
            self._stage_samples = None
        # HistoryBackend has already synchronized the prefix and denoise. Reading
        # these events here adds no synchronization inside the model pipeline.
        for name, intervals in samples.items():
            timings[name] = (sum(a.elapsed_time(b) for a, b in intervals)
                             if self.device.type == 'cuda' else sum(intervals))
        return result, timings

    def _encode_view(self, name, pixels, grid):
        visual = self.model.qwen.model.visual
        key = tuple(map(tuple, grid.tolist()))
        cached = self._vision_layouts.get(name)
        if cached is None or cached[0] != key:
            cached = (key, vision_constants(visual, grid))
            self._vision_layouts[name] = cached
        pixels = torch.as_tensor(pixels)
        pixels = pixels.reshape(-1, pixels.shape[-1]).to(self.device, dtype=torch.bfloat16, non_blocking=True)
        return encode_vision(visual, pixels, cached[1])

    def _prefill(self, observation, ids):
        model = self.model
        layout = prompt_layout(model, ids, observation.image_grid, observation.video_grid)
        device = self.device
        with self._time_stage('action_lm_prefill_ms'):
            embeds = model.qwen.get_input_embeddings()(layout['ids'].to(device, non_blocking=True))
            slots = layout['slots']

            def fill(name, values):
                embeds.index_copy_(0, slots[name].to(device, non_blocking=True), values.to(embeds.dtype))

            if slots['marker'].numel():
                fill('marker', model.context_marker_embeddings(layout['marker_index'].to(device, non_blocking=True)))
            fill('image', observation.images)
            if observation.video is not None:
                fill('video', observation.video)
            fill('state', model.state_projection(observation.state.to(model.state_projection.weight.dtype)))

            positions = layout['positions'].to(device, non_blocking=True)
            mask = torch.ones((1, len(ids)), dtype=torch.bool, device=device)
            output = model.qwen.model.language_model(
                inputs_embeds=embeds[None], attention_mask=mask, position_ids=positions,
                cache_position=torch.arange(len(ids), device=device), use_cache=True, return_dict=True)
            if output.past_key_values is None:
                raise RuntimeError('Qwen3.5 did not return a KV cache during prefill')
            deltas = layout['deltas'].to(device, non_blocking=True)
            return output.last_hidden_state[:, -1], output.past_key_values, mask, deltas

    @torch.no_grad()
    def eager_prefix(self, model_input):
        from lerobot.policies.qwen3_5_context_video_ki.modeling_qwen3_5_context_video_ki import (
            PIXEL_VALUES_VIDEOS_KEY, VIDEO_GRID_THW_KEY, _flat_video_grid, _inference_autocast,
        )
        model = self.model
        prompt = semantic_prompt(model, model_input)
        image_grid = torch.as_tensor(model_input['observation.image_grid_thw']).long().reshape(-1, 3)
        video_grid = _flat_video_grid(model_input.get(VIDEO_GRID_THW_KEY))
        state = torch.as_tensor(model_input['observation.state'])
        if state.shape != (model.config.max_state_dim,):
            raise ValueError('state does not match the configured prefix projection')
        state = state[None].to(self.device, dtype=torch.bfloat16, non_blocking=True)
        with _inference_autocast(model):
            with self._time_stage('action_camera_ms'):
                images = self._encode_view('image', model_input['observation.pixel_values'], image_grid)
            video = None
            if video_grid is not None:
                with self._time_stage('action_history_video_ms'):
                    video = self._encode_view('video', model_input[PIXEL_VALUES_VIDEOS_KEY], video_grid)
            observation = PrefixObservation(state, image_grid, video_grid, images, video)
            if self.capture_refresh:
                forced, _ = model._language_request(model_input, 1)
                self._refresh = (observation, prompt, forced[0], self._cold_observation)
                self._cold_observation = None
            if self.language_ids:
                prompt = [*prompt, model.special_tokens.subtask_start, *self.language_ids,
                          model.special_tokens.subtask_end]
            _, kv, mask, deltas = self._prefill(observation, prompt)
            anchor = model._cache_tail_positions(mask, deltas)
        self._pending_subtask = self.subtask
        return kv, mask, anchor, state.float()

    def graph_prefix_capacity(self, observed):
        # Our warmup uses a cold action-only prompt; reserve room for a completed language segment.
        capacity = super().graph_prefix_capacity(observed)
        return capacity + self.model_config.max_subtask_tokens if self.generate_language else capacity

    def refresh_language(self):
        refresh, self._refresh = self._refresh, None
        if refresh is None:
            raise RuntimeError('language refresh has no action observation')
        return self._decode_language(*refresh, submitted=time.perf_counter())

    def _decode_language(self, prepared, prompt, forced, cold_observation, *, submitted):
        started = time.perf_counter()
        prepare_ms = 0.
        prefill, decode = [], []
        cuda = self.device.type == 'cuda'
        from lerobot.policies.qwen3_5_context_video.tokenization_qwen3_5_context_video import parse_language_segment
        from lerobot.policies.qwen3_5_context_video_ki.modeling_qwen3_5_context_video_ki import _inference_autocast
        model = self.model
        if cold_observation is not None:
            # The cold action prompt had a different Output header. Build the language
            # header on the worker after the action, keeping the pooled vision tensors.
            inputs = super().build_input(*cold_observation, generate=self.generate_language)
            prompt = semantic_prompt(model, inputs)
            forced = model._language_request(inputs, 1)[0][0]
            del inputs, cold_observation
            prepare_ms = (time.perf_counter() - started) * 1000
            yield
        with _timed_slice(cuda, prefill), torch.no_grad(), _inference_autocast(model):
            hidden, kv, mask, deltas = self._prefill(prepared, [*prompt, model.special_tokens.subtask_start, *forced])
        del prepared
        yield
        tokens = list(forced)
        for _ in range(model.config.max_subtask_tokens + 1):
            # Leave autocast/no_grad before yielding to an unrelated action on this worker.
            with _timed_slice(cuda, decode), torch.no_grad(), _inference_autocast(model):
                next_ids = model._greedy_subtask_tokens(hidden)
                token = int(next_ids.item())
                if token == model.special_tokens.subtask_end:
                    break
                tokens.append(token)
                hidden, kv, mask = model._decode_generation_step(
                    token_ids=next_ids, past_key_values=kv, attention_mask=mask, rope_deltas=deltas)
            yield
        else:
            raise RuntimeError('Generation did not emit subtask_end within its token budget')
        if cuda:
            # Per-slice events exclude actions that ran between yields. Only
            # the terminal event needs to be synchronized to read timings.
            decode[-1][1].synchronize()
            prefill_ms = prefill[0][0].elapsed_time(prefill[0][1])
            decode_ms = sum(start.elapsed_time(end) for start, end in decode)
        else:
            prefill_ms, decode_ms = sum(prefill), sum(decode)
        post_started = time.perf_counter()
        fields = parse_language_segment(model.decode_subtasks([torch.tensor(tokens)])[0])
        self.language_ids = tuple(tokens)
        self.subtask = fields.get('Subtask', '')[:512] if 'Subtask' in self.generate_language else ''
        finished = time.perf_counter()
        self._monitor_update(subtask=self.subtask, subtask_ms=(finished - submitted) * 1000,
                             subtask_queue_ms=(started - submitted) * 1000, subtask_prepare_ms=prepare_ms,
                             subtask_prefill_ms=prefill_ms, subtask_decode_ms=decode_ms,
                             subtask_post_ms=(finished - post_started) * 1000)

    def reset_context(self):
        self.language_ids = ()
        self.subtask = ''
        self._cold_observation = self._refresh = None
        super().reset_context()
        self._monitor_update(**EMPTY_SUBTASK_TIMINGS)

    def close(self):
        self._vision_layouts.clear()
        self._cold_observation = self._refresh = None
        super().close()
