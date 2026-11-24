"""Compare A2's CPU input path and packed attention with the history policy."""
import importlib
from types import SimpleNamespace

import numpy as np
import pytest

from test_a2_optimized import entrypoint
from test_qwen35_history_backend import isolated_vendor
from test_qwen35_history_graphs import patch_module


def load_a2(monkeypatch, visual, processor):
    from rc_qwen35_history.backend import HistoryBackend
    entrypoint()
    backend = importlib.import_module('_a2_test.backend').OptimizedBackend(
        {'model': {'checkpoint': '/unused'}, 'inference': {'generate_language': ['Subtask']}})
    backend.model = SimpleNamespace(qwen=SimpleNamespace(model=SimpleNamespace(visual=visual)))
    backend.processor = processor
    monkeypatch.setattr(HistoryBackend, 'load', lambda self: None)
    backend.load()
    return backend


@pytest.mark.usefixtures('isolated_vendor')
@pytest.mark.parametrize('count', [0, 1, 5, 32])
def test_history_frames_are_normalized_together(monkeypatch, count):
    import torch
    from rc_qwen35_history.history import HistoryFrame
    from lerobot.policies.qwen3_5_context_video.transform_qwen3_5_context_video import CONTEXT_SLOTS_KEY
    from lerobot.policies.qwen3_5_context_video_ki.transform_qwen3_5_context_video_ki import (
        Qwen3_5ContextVideoKIProcessorTransformFn,
    )
    processor = Qwen3_5ContextVideoKIProcessorTransformFn(context_slot_count=32)
    backend = load_a2(monkeypatch, SimpleNamespace(), processor)
    backend.prepare_sample = lambda *args: {}
    def build(sample, *, contexts, **kwargs):
        return processor._clip_content(processor._slots({CONTEXT_SLOTS_KEY: contexts}))
    monkeypatch.setattr(processor, 'build_inference', build)
    # Include non-contiguous, immutable CHW images and distinct timestamps.
    frames = tuple(HistoryFrame(15*i, (np.arange(90, dtype=np.uint8) + i).reshape(5, 6, 3).transpose(2, 0, 1))
                   for i in range(count))
    for item in frames:
        item.image.flags.writeable = False
    calls = []
    stack = np.stack
    def counted(images, *args, **kwargs):
        calls.append([image.dtype for image in images])
        return stack(images, *args, **kwargs)
    monkeypatch.setattr(np, 'stack', counted)
    _, videos, metadata, _ = backend.build_input(None, {}, 'task', frames)
    assert calls == ([[np.uint8] * count] if count else [])
    if count:
        expected = torch.stack([torch.from_numpy(item.image.copy()).float()/255 for item in frames])
        assert torch.equal(videos[0], expected)
        assert videos[0].dtype == torch.float32 and videos[0].is_contiguous()
        assert metadata[0].frames_indices == [item.index for item in frames]
        videos[0].zero_()
        assert int(frames[0].image.max()) == 89
    else:
        assert not videos and not metadata


@pytest.mark.usefixtures('isolated_vendor')
@pytest.mark.parametrize('dtype', ['float32', 'bfloat16'])
@pytest.mark.parametrize('grid', [
    [[1, 4, 4]],
    [[1, 4, 4]] * 3,
    [[16, 4, 4]],
    [[1, 4, 4], [1, 4, 6], [2, 4, 4]],
])
def test_vision_batches_one_sdpa_per_distinct_length(monkeypatch, patch_module, grid, dtype):
    import torch
    from rc_qwen35_history.graphs import PrefixGraph
    from transformers.models.qwen3_5.configuration_qwen3_5 import Qwen3_5VisionConfig

    torch.manual_seed(53)
    config = Qwen3_5VisionConfig(depth=2, hidden_size=32, intermediate_size=48, num_heads=4,
                                  out_hidden_size=32, patch_size=2, temporal_patch_size=2,
                                  spatial_merge_size=2, num_position_embeddings=64)
    config._attn_implementation = 'sdpa'
    visual = patch_module.Qwen3_5VisionModel(config).to(getattr(torch, dtype)).eval()
    pixels = torch.randn(sum(t*h*w for t,h,w in grid), 24, dtype=visual.dtype)
    sizes = torch.tensor(grid)
    calls = []
    sdpa = torch.nn.functional.scaled_dot_product_attention
    def counted(q, k, v, *args, **kwargs):
        calls.append((q.shape[0], q.shape[2]))
        return sdpa(q, k, v, *args, **kwargs)
    monkeypatch.setattr(torch.nn.functional, 'scaled_dot_product_attention', counted)
    with torch.no_grad():
        reference = visual(pixels, sizes).pooler_output
        assert len(calls) == config.depth * sum(t for t, _, _ in grid)
        load_a2(monkeypatch, visual, SimpleNamespace())
        lengths = [h*w for t,h,w in grid for _ in range(t)]
        groups = sorted((lengths.count(length), length) for length in set(lengths))
        calls.clear()
        result = visual(pixels, sizes).pooler_output
        assert sorted(calls) == sorted(groups * config.depth)
        torch.testing.assert_close(result, reference)
        # The prefix-graph path invokes the visual blocks directly, bypassing visual.forward.
        graph = PrefixGraph.__new__(PrefixGraph)
        graph.visual, graph.ids = visual, torch.empty(0)
        constants = graph._vision_constants(sizes)
        calls.clear()
        torch.testing.assert_close(graph._vision(pixels, constants), reference)
        assert sorted(calls) == sorted(groups * config.depth)


@pytest.mark.usefixtures('isolated_vendor')
def test_batched_vision_cuda_graph_replays_new_pixels(monkeypatch, patch_module):
    import torch
    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        pytest.skip('requires a CUDA device with bfloat16 support')
    from rc_qwen35_history.graphs import PrefixGraph
    from transformers.models.qwen3_5.configuration_qwen3_5 import Qwen3_5VisionConfig

    config = Qwen3_5VisionConfig(depth=1, hidden_size=32, intermediate_size=48, num_heads=4,
                                  out_hidden_size=32, patch_size=2, temporal_patch_size=2,
                                  spatial_merge_size=2, num_position_embeddings=64)
    config._attn_implementation = 'sdpa'
    visual = patch_module.Qwen3_5VisionModel(config).to(device='cuda', dtype=torch.bfloat16).eval()
    sizes = torch.tensor([[4, 4, 4]])
    inputs = torch.randn(2, 64, 24, device='cuda', dtype=torch.bfloat16)
    with torch.no_grad():
        expected = [visual(pixels, sizes.cuda()).pooler_output for pixels in inputs]
        load_a2(monkeypatch, visual, SimpleNamespace())
        prefix = PrefixGraph.__new__(PrefixGraph)
        prefix.visual, prefix.ids = visual, torch.empty(0, device='cuda')
        constants = prefix._vision_constants(sizes)
        static = inputs[0].clone()
        stream = torch.cuda.Stream()
        stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(stream):
            prefix._vision(static, constants)
        torch.cuda.current_stream().wait_stream(stream)
        graph = torch.cuda.CUDAGraph()
        with torch.cuda.graph(graph):
            result = prefix._vision(static, constants)
        for pixels, baseline in zip(inputs, expected):
            static.copy_(pixels)
            graph.replay()
            torch.testing.assert_close(result, baseline, rtol=.02, atol=.001)
