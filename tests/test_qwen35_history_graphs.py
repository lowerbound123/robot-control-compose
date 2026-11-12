"""Isolated source-patch numerics and capture-safe allocation without weights."""
import importlib.util
import sys

import pytest
from history_helpers import ROOT

torch = pytest.importorskip('torch')
pytest.importorskip('transformers')


@pytest.fixture(scope='module')
def patch_module():
    path = ROOT / 'policy/qwen35_history/vendor/lerobot/policies/qwen3_5vla/transformers_replace/models/qwen3_5/modeling_qwen3_5.py'
    name = 'transformers.models.qwen3_5._rc_history_patch'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(name, None)


def run(module, kind, inputs):
    function = getattr(module, f'torch_{kind}_gated_delta_rule')
    kwargs = {'chunk_size': 2} if kind == 'chunk' else {}
    return function(*inputs, initial_state=None, output_final_state=True, **kwargs)


@pytest.mark.parametrize('kind', ['chunk', 'recurrent'])
def test_fallback_allocates_on_input_device_without_host_copy(patch_module, kind):
    from torch.utils._python_dispatch import TorchDispatchMode
    from torch.utils._pytree import tree_leaves
    class NoHost(TorchDispatchMode):
        def __torch_dispatch__(self, func, types, args=(), kwargs=None):
            result = func(*args, **(kwargs or {}))
            assert all(v.device.type != 'cpu' for v in tree_leaves(result) if isinstance(v,torch.Tensor)), str(func)
            return result
    q = torch.empty(1,3,1,2,device='meta',dtype=torch.bfloat16)
    v = torch.empty(1,3,1,4,device='meta',dtype=torch.bfloat16)
    t = torch.empty(1,3,1,device='meta')
    with NoHost():
        output, state = run(patch_module, kind, (q,q,v,t,t))
    assert output.shape == (1,3,1,4) and state.shape == (1,1,2,4)


@pytest.mark.parametrize('kind', ['chunk', 'recurrent'])
def test_fallback_matches_hand_computed_recurrence(patch_module, kind):
    def vector(values): return torch.tensor(values,dtype=torch.float32).reshape(1,3,1,1)
    args = (vector([1,2,1]),vector([1,.5,2]),vector([2,4,3]),
            torch.tensor([.5,.25,.5]).log().reshape(1,3,1),torch.tensor([.5,.25,.5]).reshape(1,3,1))
    output, state = run(patch_module, kind, args)
    torch.testing.assert_close(output.flatten(),torch.tensor([1.,1.46875,2.6328125]))
    torch.testing.assert_close(state.flatten(),torch.tensor([2.6328125]))


@pytest.mark.skipif(not torch.cuda.is_available(), reason='requires NVIDIA CUDA; CPU numerics do not validate capture')
def test_real_cuda_replay_changes_with_input(patch_module):
    q = torch.ones(1,3,1,2,device='cuda')
    v = torch.ones(1,3,1,4,device='cuda')
    g = torch.zeros(1,3,1,device='cuda')
    beta = torch.full_like(g,.25)
    stream = torch.cuda.Stream()
    stream.wait_stream(torch.cuda.current_stream())
    with torch.cuda.stream(stream):
        for _ in range(2): run(patch_module,'chunk',(q,q,v,g,beta))
    torch.cuda.current_stream().wait_stream(stream)
    graph = torch.cuda.CUDAGraph()
    with torch.cuda.graph(graph):
        actual,_ = run(patch_module,'chunk',(q,q,v,g,beta))
    for value in (2.,3.):
        v.fill_(value)
        graph.replay()
        expected,_ = run(patch_module,'chunk',(q,q,v,g,beta))
        torch.testing.assert_close(actual,expected)
