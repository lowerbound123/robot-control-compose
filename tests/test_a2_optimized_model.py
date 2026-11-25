"""Exercise the deployed prompt/feature path using small deterministic CPU weights."""
import importlib
from types import SimpleNamespace

import pytest

from test_a2_optimized import entrypoint
from test_a2_optimized_load import checkpoint
from test_qwen35_history_backend import isolated_vendor
from test_qwen35_history_language_backend import tiny_language_model


@pytest.mark.usefixtures('isolated_vendor')
@pytest.mark.parametrize('has_history', [False, True])
def test_refresh_reuses_visual_features_and_actions_receive_exact_cached_tokens(has_history):
    torch = pytest.importorskip('torch')
    entrypoint()
    module = importlib.import_module('lerobot.policies.qwen3_5_context_video_ki.modeling_qwen3_5_context_video_ki')
    backend_cls = importlib.import_module('_a2_test.backend').OptimizedBackend
    model, _, calls = tiny_language_model(module)
    # Restore the production semantic-prefix extraction and multimodal embedding.
    del model._prepare_generation_prefix, model.embed_prefix
    model.config.max_state_dim = 2
    model.state_projection = torch.nn.Linear(2, 3)
    model.qwen.config = SimpleNamespace(image_token_id=17, video_token_id=18)
    encoded = []
    model.qwen.model.visual = SimpleNamespace(spatial_merge_size=1)
    rows = []

    def encode_view(name, pixels, grid):
        encoded.append(name)
        return pixels
    def positions(ids, grid, mask, **kwargs):
        rows.append(ids[0].tolist())
        return torch.arange(ids.shape[1]).reshape(1, 1, -1).expand(4, 1, -1), torch.zeros(1, 1, dtype=torch.long)
    model._build_position_ids = positions
    model.__dict__['_tokenizer'] = SimpleNamespace(batch_decode=lambda *args, **kwargs: ['Subtask: pull cable'])
    backend = backend_cls({'model': {'checkpoint': '/unused'}, 'inference': {'generate_language': ['Subtask']}})
    backend.model, backend.device = model, torch.device('cpu')
    backend._encode_view = encode_view
    backend.prepare_sample = lambda *args: {}
    tokens = model.special_tokens
    headers = []
    def process(sample, *, generate, **kwargs):
        headers.append(generate)
        row = [11 if generate else 10, 17, 17, 17, *([18] if has_history else []), tokens.learned_state, tokens.action_image]
        batch = {'observation.input_ids': torch.tensor(row), 'observation.attention_mask': torch.ones(len(row)),
                 'observation.state': torch.zeros(2), 'observation.pixel_values': torch.ones(3, 3),
                 'observation.image_grid_thw': torch.ones(3, 3, dtype=torch.long),
                 module.LANGUAGE_IDS_KEY: torch.tensor([9]), module.LANGUAGE_LENGTH_KEY: torch.tensor(int(bool(generate))),
                 module.LANGUAGE_GENERATE_KEY: torch.tensor(bool(generate))}
        if has_history:
            batch[module.PIXEL_VALUES_VIDEOS_KEY] = torch.full((1, 3), 2.)
            batch[module.VIDEO_GRID_THW_KEY] = torch.ones(1, 3, dtype=torch.long)
        return batch
    backend.processor = SimpleNamespace(build_inference=process)
    encoders = ['image', 'video'] if has_history else ['image']
    # Cold fourth action, then its refresh with the proper language header.
    backend.capture_refresh = True
    backend.eager_prefix(backend.build_input(None, {}, 'task', ()))
    action_prefix = calls[-1]['inputs_embeds'].clone()
    assert encoded == encoders and not backend.language_ids and headers == [()]
    list(backend.refresh_language())
    assert backend.language_ids == (9, 100, 101)
    assert backend.monitor()['subtask'] == 'pull cable'
    cold = backend.monitor()
    assert cold['subtask_prepare_ms'] > 0
    assert cold['subtask_prefill_ms'] > 0 and cold['subtask_decode_ms'] > 0
    assert cold['subtask_ms'] >= sum(cold[key] for key in (
        'subtask_queue_ms', 'subtask_prepare_ms', 'subtask_prefill_ms', 'subtask_decode_ms', 'subtask_post_ms'))
    assert encoded == encoders and headers == [(), ('Subtask',)]
    torch.testing.assert_close(action_prefix[:, 1:], calls[1]['inputs_embeds'][:, 1:action_prefix.shape[1]])
    # Warm refresh uses the same processor result for both prompts and the cached closed segment for the action.
    backend.eager_prefix(backend.build_input(None, {}, 'task', ()))
    assert rows[-1][-5:] == [tokens.subtask_start, 9, 100, 101, tokens.subtask_end]
    list(backend.refresh_language())
    assert encoded == encoders * 2 and headers == [(), ('Subtask',), ('Subtask',)]
    assert backend.monitor()['subtask_prepare_ms'] == 0
    backend.reset_context()
    assert backend.language_ids == () and backend.monitor()['subtask'] == ''
    assert backend.monitor()['subtask_ms'] == 0


@pytest.mark.usefixtures('isolated_vendor')
@pytest.mark.parametrize('device', ['cpu', 'cuda'])
def test_eager_prefix_reuses_grid_constants_and_matches_reference(checkpoint, device, monkeypatch):
    import torch
    from lerobot.policies.qwen3_5_context_video_ki import modeling_qwen3_5_context_video_ki as ki
    from rc_qwen35_history.graphs import euler_reference

    if device == 'cuda' and not torch.cuda.is_available():
        pytest.skip('requires CUDA')
    backend, _, _, reference = checkpoint
    backend.device = torch.device(device)
    model = backend.model = reference.to(backend.device).eval().model
    visual = model.qwen.model.visual
    visual.batch_equal_slices = True
    cfg = model.qwen.config
    cfg.vision_start_token_id, cfg.image_token_id, cfg.video_token_id = 12, 13, 14
    patch_dim = visual.patch_embed.proj.weight[0].numel()
    cached_grids = []
    interpolate = visual.fast_pos_embed_interpolate

    def counted(grid):
        cached_grids.append(grid.tolist())
        return interpolate(grid)

    monkeypatch.setattr(visual, 'fast_pos_embed_interpolate', counted)
    generator = torch.Generator().manual_seed(73)
    noise = torch.randn(1, model.config.chunk_size, model.config.max_action_dim, generator=generator).to(device)
    sampled = []

    def sample(kv, mask, anchor, state):
        sampled.append((kv, mask, anchor, state))
        return euler_reference(model, 2, kv, mask, anchor, state, noise)

    backend.sampler = SimpleNamespace(sample=sample)
    backend.finalize_actions = lambda prediction, qpos: prediction
    if device == 'cpu':
        monkeypatch.setattr(torch.cuda, 'synchronize', lambda *args: None)
    for index, depth in enumerate((0, 1, 1, 2)):
        row = [10, 12, *([13] * 4), *([22] * index), ki.CTX_TOKEN_START, *([12, 14] * depth),
               model.special_tokens.learned_state, model.special_tokens.action_image]
        ids = torch.tensor([*row, 0, 0])
        inputs = {'observation.input_ids': ids, 'observation.attention_mask': ids.ne(0),
                  'observation.state': torch.randn(model.config.max_state_dim, generator=generator),
                  'observation.image_grid_thw': torch.tensor([[1, 4, 4]]),
                  'observation.pixel_values': torch.randn(16, patch_dim, generator=generator),
                  ki.VIDEO_GRID_THW_KEY: torch.tensor([[depth, 2, 2]]) if depth else torch.empty(0, 3),
                  ki.PIXEL_VALUES_VIDEOS_KEY: torch.randn(depth * 4, patch_dim, generator=generator)}
        backend.generate_language = ('Subtask',) if index else ()
        backend.language_ids = (20, 21) if index else ()
        batch = backend.model_batch(inputs)
        with torch.no_grad(), ki._inference_autocast(model):
            prepared, (prompt,) = model._prepare_generation_prefix(batch, requested_order=ki.ORDER_ACTION_FIRST)
            if backend.language_ids:
                prompt += [model.special_tokens.subtask_start, *backend.language_ids, model.special_tokens.subtask_end]
                prepared['observation.input_ids'], prepared['observation.attention_mask'] = (
                    model._left_pad_prefixes([prompt], backend.device))
            _, expected_kv, mask, _, deltas = model._prefill_generation(prepared)
            anchor = model._cache_tail_positions(mask, deltas)
        with torch.no_grad():
            expected_actions = euler_reference(model, 2, expected_kv, mask, anchor, batch['observation.state'].float(), noise)
        backend.build_input = lambda *args: inputs
        cached_grids.clear()
        actual_actions, timings = backend.predict(None, None, 'task', ())
        actual_kv, actual_mask, actual_anchor, actual_state = sampled.pop()
        torch.testing.assert_close(actual_actions, expected_actions, rtol=0, atol=0)
        assert timings['action_camera_ms'] > 0 and timings['action_lm_prefill_ms'] > 0
        assert (timings['action_history_video_ms'] > 0) == bool(depth)
        assert len(cached_grids) == (1, 1, 0, 1)[index]
        torch.testing.assert_close(actual_mask, mask)
        torch.testing.assert_close(actual_anchor, anchor)
        torch.testing.assert_close(actual_state, batch['observation.state'].float(), rtol=0, atol=0)
        for field in ('key_cache', 'value_cache', 'conv_states', 'recurrent_states'):
            for actual, expected in zip(getattr(actual_kv, field), getattr(expected_kv, field), strict=True):
                if expected is not None:
                    torch.testing.assert_close(actual, expected, rtol=0, atol=0)
