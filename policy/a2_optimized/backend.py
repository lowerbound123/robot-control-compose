"""History actions conditioned on the latest complete language segment."""
import torch

from rc_qwen35_history.backend import HistoryBackend

IDS = 'observation.input_ids'
MASK = 'observation.attention_mask'
PIXELS = 'observation.pixel_values'


class OptimizedBackend(HistoryBackend):
    capture_refresh = False

    def __init__(self, config, **kwargs):
        super().__init__(config, **kwargs)
        self.language_ids = ()
        self.subtask = ''
        self._cold_observation = self._refresh = None

    def build_input(self, qpos, images, instruction, contexts):
        observation = (qpos, images, instruction, contexts)
        # With an empty cache the action prompt must announce only actions.
        self._cold_observation = observation if self.capture_refresh and not self.language_ids else None
        return super().build_input(*observation, generate=self.generate_language if self.language_ids else ())

    def _prefill(self, prepared, ids):
        tokens, mask = self.model._left_pad_prefixes([ids], self.device)
        return self.model._prefill_generation({**prepared, IDS: tokens, MASK: mask})

    @torch.no_grad()
    def eager_prefix(self, model_input):
        from lerobot.policies.qwen3_5_context_video_ki.modeling_qwen3_5_context_video_ki import (
            ORDER_ACTION_FIRST, PIXEL_VALUES_VIDEOS_KEY, VIDEO_GRID_THW_KEY, _inference_autocast,
        )
        if not self.generate_language:
            return super().eager_prefix(model_input)
        model = self.model
        batch = self.model_batch(model_input)
        prepared, (prompt,) = model._prepare_generation_prefix(batch, requested_order=ORDER_ACTION_FIRST)
        with _inference_autocast(model):
            if self.capture_refresh:
                # The two prompts have different language tails but identical camera inputs.
                prepared['visual_features'] = model.encode_visual_features(
                    prepared.pop(PIXELS), prepared['observation.image_grid_thw'],
                    prepared.pop(PIXEL_VALUES_VIDEOS_KEY, None), prepared.get(VIDEO_GRID_THW_KEY))
                forced, _ = model._language_request(batch, 1)
                self._refresh = self._decode_language(prepared, prompt, forced[0], self._cold_observation)
                self._cold_observation = None
            if self.language_ids:
                action_prompt = [*prompt, model.special_tokens.subtask_start, *self.language_ids,
                                 model.special_tokens.subtask_end]
                result = self._prefill(prepared, action_prompt)
            else:
                result = model._prefill_generation(prepared)
            _, kv, mask, _, deltas = result
            anchor = model._cache_tail_positions(mask, deltas)
        self._pending_subtask = self.subtask
        return kv, mask, anchor, batch['observation.state'].float()

    def graph_prefix_capacity(self, observed):
        # Our warmup uses a cold action-only prompt; reserve room for a completed language segment.
        capacity = super().graph_prefix_capacity(observed)
        return capacity + self.model_config.max_subtask_tokens if self.generate_language else capacity

    def refresh_language(self):
        refresh, self._refresh = self._refresh, None
        if refresh is None:
            raise RuntimeError('language refresh has no action observation')
        return refresh

    def _decode_language(self, prepared, prompt, forced, cold_observation):
        from lerobot.policies.qwen3_5_context_video.tokenization_qwen3_5_context_video import parse_language_segment
        from lerobot.policies.qwen3_5_context_video_ki.modeling_qwen3_5_context_video_ki import _inference_autocast
        model = self.model
        if cold_observation is not None:
            # The cold action prompt had a different Output header. Build the language
            # header on the worker after the action, keeping the pooled vision tensors.
            inputs = super().build_input(*cold_observation, generate=self.generate_language)
            ids = inputs[IDS].reshape(1, -1)
            mask = inputs[MASK].reshape_as(ids).bool()
            prompt = model._extract_semantic_prefix(ids, mask, 0, selected_mode_token=model.special_tokens.action_image)
            forced = model._language_request(inputs, 1)[0][0]
            del inputs, cold_observation
            yield
        with torch.no_grad(), _inference_autocast(model):
            hidden, kv, mask, _, deltas = self._prefill(prepared, [*prompt, model.special_tokens.subtask_start, *forced])
        del prepared
        yield
        tokens = list(forced)
        for _ in range(model.config.max_subtask_tokens + 1):
            # Leave autocast/no_grad before yielding to an unrelated action on this worker.
            with torch.no_grad(), _inference_autocast(model):
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
        fields = parse_language_segment(model.decode_subtasks([torch.tensor(tokens)])[0])
        self.language_ids = tuple(tokens)
        self.subtask = fields.get('Subtask', '')[:512] if 'Subtask' in self.generate_language else ''
        self._monitor_update(subtask=self.subtask)

    def reset_context(self):
        self.language_ids = ()
        self.subtask = ''
        self._cold_observation = self._refresh = None
        super().reset_context()

    def close(self):
        self._cold_observation = self._refresh = None
        super().close()
