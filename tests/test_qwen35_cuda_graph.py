"""Exercise the installed Qwen patch without model weights or optional FLA kernels."""
import pytest

torch = pytest.importorskip('torch')
pytest.importorskip('transformers')

from torch.utils._python_dispatch import TorchDispatchMode
from torch.utils._pytree import tree_leaves
from transformers.models.qwen3_5.modeling_qwen3_5 import (
    torch_chunk_gated_delta_rule,
    torch_recurrent_gated_delta_rule,
)


def run_rule(kind, inputs, *, initial_state=None, output_final_state=True, normalize=False):
    options = dict(initial_state=initial_state, output_final_state=output_final_state,
                   use_qk_l2norm_in_kernel=normalize)
    if kind == 'chunk':
        return torch_chunk_gated_delta_rule(*inputs, chunk_size=2, **options)
    return torch_recurrent_gated_delta_rule(*inputs, **options)


class RejectHostTensors(TorchDispatchMode):
    """Check the allocation boundary on a non-CPU device without requiring CUDA."""
    def __torch_dispatch__(self, func, types, args=(), kwargs=None):
        result = func(*args, **(kwargs or {}))
        for value in tree_leaves(result):
            if isinstance(value, torch.Tensor):
                assert value.device.type != 'cpu', f'host tensor created by {func}'
        return result


@pytest.mark.parametrize('kind', ['chunk', 'recurrent'])
@pytest.mark.parametrize('with_state', [False, True])
def test_fallback_temporaries_stay_on_the_input_device(kind, with_state):
    # Meta tensors run the real operators' shape/device paths. This catches a
    # CPU factory followed by .to(value), which cannot be used in CUDA capture.
    query = torch.empty(2, 5, 2, 3, device='meta', dtype=torch.bfloat16)
    key = torch.empty_like(query)
    value = torch.empty(2, 5, 2, 4, device='meta', dtype=torch.bfloat16)
    g = torch.empty(2, 5, 2, device='meta')
    beta = torch.empty_like(g)
    initial = torch.empty(2, 2, 3, 4, device='meta', dtype=torch.float64) if with_state else None
    with RejectHostTensors():
        output, state = run_rule(kind, (query, key, value, g, beta), initial_state=initial, normalize=True)
    assert output.shape == (2, 5, 2, 4)
    assert output.device.type == state.device.type == 'meta'
    assert output.dtype == torch.bfloat16
    assert state.shape == (2, 2, 3, 4) and state.dtype == torch.float32


def scalar_inputs(device='cpu', dtype=torch.float32):
    def vector(values):
        return torch.tensor(values, device=device, dtype=dtype).reshape(1, 3, 1, 1)
    query, key, value = vector([1, 2, 1]), vector([1, .5, 2]), vector([2, 4, 3])
    g = torch.tensor([.5, .25, .5], device=device).log().reshape(1, 3, 1)
    beta = torch.tensor([.5, .25, .5], device=device).reshape(1, 3, 1)
    return query, key, value, g, beta


@pytest.mark.parametrize('kind', ['chunk', 'recurrent'])
@pytest.mark.parametrize('initial_value,expected', [
    (None, [1, 1.46875, 2.6328125]),
    (1, [1.25, 1.5859375, 2.603515625]),
])
def test_fallback_keeps_hand_calculated_delta_rule_outputs(kind, initial_value, expected):
    # One-dimensional state: S <- exp(g)*S; S <- S + k*beta*(v-k*S);
    # output=q*S. Literal expected values cover cross-chunk state and padding.
    initial = torch.full((1, 1, 1, 1), float(initial_value)) if initial_value is not None else None
    output, state = run_rule(kind, scalar_inputs(), initial_state=initial)
    torch.testing.assert_close(output.flatten(), torch.tensor(expected))
    torch.testing.assert_close(state.flatten(), torch.tensor([expected[-1]]))
    if initial is not None:
        torch.testing.assert_close(initial, torch.ones_like(initial))
    without_state, final_state = run_rule(kind, scalar_inputs(), initial_state=initial, output_final_state=False)
    torch.testing.assert_close(without_state, output)
    assert final_state is None


@pytest.mark.skipif(not torch.cuda.is_available(), reason='requires an NVIDIA CUDA device for graph capture/replay')
@pytest.mark.parametrize('kind', ['chunk', 'recurrent'])
@pytest.mark.parametrize('dtype', [torch.float32, torch.bfloat16])
def test_fallback_cuda_graph_replays_with_new_input_values(kind, dtype):
    if dtype == torch.bfloat16 and not torch.cuda.is_bf16_supported():
        pytest.skip('CUDA device does not support bfloat16')
    inputs = scalar_inputs('cuda', dtype)
    initial = torch.ones(1, 1, 1, 1, device='cuda') if kind == 'recurrent' else None
    stream = torch.cuda.Stream()
    stream.wait_stream(torch.cuda.current_stream())
    with torch.cuda.stream(stream):
        for _ in range(3):
            run_rule(kind, inputs, initial_state=initial)
    torch.cuda.current_stream().wait_stream(stream)
    graph = torch.cuda.CUDAGraph()
    with torch.cuda.graph(graph):
        output, state = run_rule(kind, inputs, initial_state=initial)
    for values in ([3, 2, 1], [1, 4, 2]):
        inputs[2].copy_(torch.tensor(values, device='cuda', dtype=dtype).reshape(1, 3, 1, 1))
        graph.replay()
        torch.cuda.synchronize()
        expected_output, expected_state = run_rule(kind, inputs, initial_state=initial)
        torch.testing.assert_close(output, expected_output)
        torch.testing.assert_close(state, expected_state)
