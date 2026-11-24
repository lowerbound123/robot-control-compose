"""Exercise the deployed prompt/feature path using small deterministic CPU weights."""
import importlib
from types import SimpleNamespace

import pytest

from test_a2_optimized import entrypoint
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
    def camera(**kwargs):
        encoded.append('image')
        return SimpleNamespace(pooler_output=kwargs['pixel_values'])
    def video(**kwargs):
        encoded.append('video')
        return SimpleNamespace(pooler_output=kwargs['pixel_values_videos'])
    model.qwen.get_image_features, model.qwen.get_video_features = camera, video
    def positions(ids, grid, mask, **kwargs):
        return torch.arange(ids.shape[1]).reshape(1, 1, -1).expand(4, 1, -1), torch.zeros(1, 1, dtype=torch.long)
    model._build_position_ids = positions
    model.__dict__['_tokenizer'] = SimpleNamespace(batch_decode=lambda *args, **kwargs: ['Subtask: pull cable'])
    backend = backend_cls({'model': {'checkpoint': '/unused'}, 'inference': {'generate_language': ['Subtask']}})
    backend.model, backend.device = model, torch.device('cpu')
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
    assert encoded == encoders and headers == [(), ('Subtask',)]
    torch.testing.assert_close(action_prefix[:, 1:], calls[1]['inputs_embeds'][:, 1:action_prefix.shape[1]])
    # Warm refresh uses the same processor result for both prompts and the cached closed segment for the action.
    original = model._left_pad_prefixes
    rows = []
    def record(ids, device):
        rows.append(ids[0][:])
        return original(ids, device)
    model._left_pad_prefixes = record
    backend.eager_prefix(backend.build_input(None, {}, 'task', ()))
    assert rows[-1][-5:] == [tokens.subtask_start, 9, 100, 101, tokens.subtask_end]
    list(backend.refresh_language())
    assert encoded == encoders * 2 and headers == [(), ('Subtask',), ('Subtask',)]
    backend.reset_context()
    assert backend.language_ids == () and backend.monitor()['subtask'] == ''
