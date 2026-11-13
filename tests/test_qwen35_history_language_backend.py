"""CPU language generation and monitoring; replace neural weights, not decode logic."""
import contextlib
import importlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from history_helpers import ROOT
from test_qwen35_history_runtime import config


@pytest.fixture(params=['qwen35_history', 'qwen35_history_lift2'])
def family(request, monkeypatch):
    pytest.importorskip('torch')
    pytest.importorskip('transformers')
    saved = {key: value for key, value in sys.modules.items() if key == 'lerobot' or key.startswith('lerobot.')}
    for key in saved:
        del sys.modules[key]
    monkeypatch.syspath_prepend(str(ROOT / 'policy' / request.param / 'vendor'))
    backend = importlib.import_module('rc_' + request.param + '.backend')
    graphs = importlib.import_module('rc_' + request.param + '.graphs')
    model = importlib.import_module('lerobot.policies.qwen3_5_context_video_ki.modeling_qwen3_5_context_video_ki')
    yield backend, graphs, model
    for key in list(sys.modules):
        if key == 'lerobot' or key.startswith('lerobot.'):
            del sys.modules[key]
    sys.modules.update(saved)


def tiny_language_model(module):
    import torch
    tokens = module.UnifiedSpecialTokenIds(*range(module.CONTROL_TOKEN_START, module.CONTROL_TOKEN_END))
    embedding = torch.nn.Embedding(module.IBQ_TOKEN_START, 3)
    head = torch.nn.Linear(3, module.IBQ_TOKEN_START, bias=False)
    with torch.no_grad():
        embedding.weight.zero_()
        head.weight.zero_()
        embedding.weight[9] = torch.tensor([1., 0., 0.])
        embedding.weight[100] = torch.tensor([0., 1., 0.])
        embedding.weight[101] = torch.tensor([0., 0., 1.])
        head.weight[100, 0] = head.weight[101, 1] = head.weight[tokens.subtask_end, 2] = 1.

    calls = []
    def language_model(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(last_hidden_state=kwargs['inputs_embeds'],
                               past_key_values=kwargs.get('past_key_values', object()))

    model = module.Qwen3_5ContextVideoKIModel.__new__(module.Qwen3_5ContextVideoKIModel)
    torch.nn.Module.__init__(model)
    model.config = SimpleNamespace(max_subtask_tokens=4, constrained_decoding=True)
    model.special_tokens, model.vocab_size = tokens, module.IBQ_TOKEN_START
    model.qwen = SimpleNamespace(get_input_embeddings=lambda: embedding, get_output_embeddings=lambda: head,
                                 model=SimpleNamespace(language_model=language_model))
    model._prepare_generation_prefix = lambda batch, **kwargs: (batch.copy(), [[10]])
    def embed(batch):
        ids, mask = batch['observation.input_ids'], batch['observation.attention_mask']
        positions = torch.arange(ids.shape[1]).reshape(1, 1, -1).expand(4, 1, -1)
        return embedding(ids), mask, positions, torch.zeros(1, 1, dtype=torch.long), ids
    model.embed_prefix = embed
    batch = {'observation.input_ids': torch.tensor([[10]]),
             'observation.attention_mask': torch.tensor([[True]]),
             'observation.state': torch.zeros(1, 32),
             module.LANGUAGE_IDS_KEY: torch.tensor([[9, 0, 0, 0]]),
             module.LANGUAGE_LENGTH_KEY: torch.tensor([1]),
             module.LANGUAGE_GENERATE_KEY: torch.tensor([True])}
    return model, batch, calls


def test_language_tokens_are_closed_and_refilled_before_action_conditioning(family):
    import torch
    _, _, module = family
    model, batch, calls = tiny_language_model(module)
    _, cache, mask, _, anchor, future, ids = model._prefix_stage(batch, requested_order=module.ORDER_ACTION_FIRST)
    assert ids[0].tolist() == [9, 100, 101]
    assert future is None and cache is not None
    assert mask.tolist() == [[True] * 6]
    torch.testing.assert_close(anchor, torch.full((4, 1, 1), 5, dtype=torch.long))
    assert len(calls) == 4  # open prefill, two cached tokens, then closed-segment prefill
    assert calls[1]['cache_position'].tolist() == [3]
    assert calls[2]['cache_position'].tolist() == [4]
    assert 'past_key_values' not in calls[-1]
    assert calls[-1]['inputs_embeds'].shape[1] == 6
    def decode(rows, **kwargs):
        assert rows == [[9, 100, 101]]
        assert kwargs == {'skip_special_tokens': True, 'clean_up_tokenization_spaces': False}
        return ['Memory: holding cup\nScene: table\nSubtask: Place cup']
    model.__dict__['_tokenizer'] = SimpleNamespace(batch_decode=decode)
    from lerobot.policies.qwen3_5_context_video.tokenization_qwen3_5_context_video import parse_language_segment
    assert parse_language_segment(model.decode_subtasks(ids)[0])['Subtask'] == 'Place cup'


def test_language_without_end_token_fails_at_the_checkpoint_budget(family):
    _, _, module = family
    model, batch, _ = tiny_language_model(module)
    model.config.max_subtask_tokens = 1
    with pytest.raises(RuntimeError, match='subtask_end'):
        model._prefix_stage(batch, requested_order=module.ORDER_ACTION_FIRST)


def test_disabled_language_keeps_original_prefix_and_no_decode(family):
    _, _, module = family
    model, batch, calls = tiny_language_model(module)
    batch.pop(module.LANGUAGE_IDS_KEY)
    result = model._prefix_stage(batch, requested_order=module.ORDER_ACTION_FIRST)
    assert result[-1] is None and result[2].shape == (1, 1)
    assert len(calls) == 1


@pytest.mark.parametrize('fields', [[], ['Memory', 'Scene', 'Subtask']])
def test_backend_passes_requested_language_to_processor(family, monkeypatch, fields):
    import torch
    backend_module, _, _ = family
    settings = config()
    settings.update(model={'checkpoint': '/unused'}, inference={'generate_language': fields})
    backend = backend_module.HistoryBackend(settings)
    monkeypatch.setattr(backend, 'prepare_sample', lambda *args: {'observation.state': torch.zeros(32)})
    backend.processor = SimpleNamespace(build_inference=lambda sample, **kwargs: kwargs)
    request = backend.build_input(np.zeros(14), {}, 'task', ())
    assert request['generate'] == tuple(fields) and request['language'] == {}


def test_backend_publishes_latest_subtask_and_clears_missing_reset_and_close(family, monkeypatch):
    import torch
    backend_module, graphs, _ = family
    settings = config()
    settings.update(model={'checkpoint': '/unused'}, inference={'generate_language': ['Memory', 'Scene', 'Subtask']})
    backend = backend_module.HistoryBackend(settings)
    backend.device, backend.chunk_size = torch.device('cpu'), 50
    backend.geometry = SimpleNamespace(max_action_dim=32)
    backend.model_config = SimpleNamespace(num_inference_steps=10)
    text = ['Memory: holding cup\nScene: table\nSubtask: Place cup on table']
    backend.model = SimpleNamespace(
        _prefix_stage=lambda *args, **kwargs: (None, object(), torch.ones(1, 6), None, torch.zeros(4, 1, 1), None, [torch.tensor([9])]),
        decode_subtasks=lambda ids: text,
        sample_noise=lambda shape, device: torch.zeros(shape))
    backend.policy = SimpleNamespace(reset=lambda: None)
    monkeypatch.setattr(backend, 'build_input', lambda *args: {'observation.state': torch.zeros(32)})
    monkeypatch.setattr(backend, 'finalize_actions', lambda *args: np.zeros((50, 14), np.float32))
    monkeypatch.setattr(graphs, '_inference_autocast', lambda _: contextlib.nullcontext())
    monkeypatch.setattr(graphs, 'euler_reference', lambda *args: torch.zeros(1, 50, 32))
    monkeypatch.setattr(torch.cuda, 'synchronize', lambda *args: None)
    def forbidden(*args):
        pytest.fail('language requests must not use the prefix graph')
    backend.prefix_graph = SimpleNamespace(prepare=forbidden)
    assert backend.monitor()['subtask'] == ''
    # Warmup uses eager_prefix directly: synthetic language must not become a live task.
    backend.eager_prefix({'observation.state': torch.zeros(32)})
    assert backend.monitor()['subtask'] == ''
    backend.predict(np.zeros(14), {}, 'task', ())
    assert backend.monitor()['subtask'] == 'Place cup on table'
    text[:] = ['Memory: holding cup\nScene: table']
    backend.predict(np.zeros(14), {}, 'task', ())
    assert backend.monitor()['subtask'] == ''
    text[:] = ['Subtask: Move away']
    backend.predict(np.zeros(14), {}, 'task', ())
    backend.reset_context()
    assert backend.monitor()['subtask'] == ''
    backend.predict(np.zeros(14), {}, 'task', ())
    backend.close()
    assert backend.monitor()['subtask'] == ''


def test_language_warmup_keeps_denoise_graph_but_skips_prefix_capture(family, monkeypatch):
    import torch
    backend_module, graphs, _ = family
    settings = config()
    settings.update(model={'checkpoint': '/unused'}, inference={
        'generate_language': ['Memory', 'Scene', 'Subtask'], 'use_cuda_graph': True, 'use_prefix_graph': True})
    backend = backend_module.HistoryBackend(settings)
    backend.device, backend.chunk_size = torch.device('cpu'), 50
    backend.history_slot_count, backend.history_slot_stride = 2, 15
    backend.geometry = SimpleNamespace(max_action_dim=32)
    backend.stats = {'states': {'mean': np.zeros(14)}}
    backend.model_config = SimpleNamespace(num_inference_steps=10, max_prompt_tokens=64)
    backend.model = SimpleNamespace(sample_noise=lambda shape, device: torch.zeros(shape))
    monkeypatch.setattr(backend, 'resize_history', lambda image: np.zeros((3, 2, 2), np.uint8))
    monkeypatch.setattr(backend, 'build_input', lambda *args: {})
    monkeypatch.setattr(backend, 'eager_prefix', lambda *args: (None, torch.ones(1, 6), None, None))
    monkeypatch.setattr(graphs, 'euler_reference', lambda *args: torch.zeros(1, 50, 32))
    class Sampler:
        def __init__(self, *args, **kwargs): self.noise = torch.zeros(1, 50, 32)
        def load(self, *args): return True
        def capture(self): pass
        def replay(self): return torch.zeros(1, 50, 32)
        def reference(self, *args): return torch.zeros(1, 50, 32)
    monkeypatch.setattr(graphs, 'FlowSampler', Sampler)
    def forbidden(*args, **kwargs):
        pytest.fail('language warmup must not capture a prefix graph')
    monkeypatch.setattr(graphs, 'capture_prefix_graphs', forbidden)
    backend.warmup()
    assert backend.sampler is not None and backend.prefix_graph is None
    assert backend.monitor()['subtask'] == ''
