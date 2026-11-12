"""Real CPU tensor transforms; no weights, robots, or accelerator required."""
import importlib
import sys

import numpy as np
import pytest
from history_helpers import ROOT, model_file

sys.path.insert(0, str(ROOT / 'policy/qwen35_history/src'))


@pytest.fixture(autouse=True)
def isolated_vendor(monkeypatch):
    # Different production containers deliberately own different lerobot trees.
    # Reproduce that import isolation when old and new CPU suites share pytest.
    saved = {k:v for k,v in sys.modules.items() if k=='lerobot' or k.startswith('lerobot.')}
    for name in saved:
        del sys.modules[name]
    monkeypatch.syspath_prepend(str(ROOT / 'policy/qwen35_history/vendor'))
    yield
    for name in list(sys.modules):
        if name=='lerobot' or name.startswith('lerobot.'):
            del sys.modules[name]
    sys.modules.update(saved)


def implementation():
    assert (ROOT / 'policy/qwen35_history/src/rc_qwen35_history/backend.py').is_file(), 'backend missing'
    pytest.importorskip('torch')
    return importlib.import_module('rc_qwen35_history.backend').HistoryBackend


def test_real_input_normalization_reorder_three_views_and_history_quantization(tmp_path):
    cls = implementation()
    from rc_qwen35_history.configuration import load_model_config
    model_file(tmp_path)
    backend = cls(load_model_config('model.json', project_root=tmp_path))
    backend.prepare_transforms()
    snapshot = backend.monitor()
    assert snapshot['normalization'] == 'mean_std' and snapshot['action_mode'] == 'delta'
    assert snapshot['current_image_long_side'] == 256 and snapshot['history_image_long_side'] == 192
    image = np.zeros((480, 640, 3), np.uint8)
    image[..., 0] = 255
    images = {n: image for n in ('head', 'left_wrist', 'right_wrist')}
    qpos = np.arange(14, dtype=np.float32)
    sample = backend.prepare_sample(qpos, images, 'task')
    expected = np.zeros(32)
    expected[[0,1,2,3,4,5,7,8,9,10,11,12,13,15]] = (qpos-1)/(2+1e-6)
    np.testing.assert_allclose(sample['observation.state'], expected)
    for index in range(3):
        tensor = sample[f'observation.images.image{index}']
        assert tuple(tensor.shape) == (3, 192, 256)
        assert float(tensor[0].min()) == 1 and float(tensor[1:].max()) == 0
    history = backend.resize_history(image)
    assert history.shape == (3,144,192) and history.dtype == np.uint8
    assert history[0].min() == 255 and history[1:].max() == 0
    np.testing.assert_array_equal(qpos, np.arange(14))


def test_inverse_reorder_then_unnormalize_then_delta_anchor(tmp_path):
    cls = implementation()
    import torch
    from rc_qwen35_history.configuration import load_model_config
    model_file(tmp_path)
    backend = cls(load_model_config('model.json', project_root=tmp_path))
    backend.prepare_transforms()
    prediction = torch.arange(32, dtype=torch.float32).repeat(1,50,1)
    qpos = np.arange(14, dtype=np.float32)
    result = backend.finalize_actions(prediction, qpos)
    expected = np.array([0,1,2,3,4,5,7,8,9,10,11,12,13,15])*(2+1e-6)+1
    expected += np.where(np.isin(np.arange(14), [6,13]), 0, qpos)
    np.testing.assert_allclose(result, np.tile(expected,(50,1)), rtol=1e-6)
    assert result.dtype == np.float32


def test_euler_uses_same_prefix_for_ten_updates_and_preserves_fp32():
    implementation()
    import contextlib
    import torch
    from rc_qwen35_history import graphs
    cache = object()
    class Model:
        calls = []
        def denoise_step(self, state, mask, kv, anchor, noisy, timestep):
            assert kv is cache
            self.calls.append(float(timestep[0]))
            return torch.full_like(noisy, 2.)
    model = Model()
    from unittest.mock import patch
    with patch.object(graphs, '_inference_autocast', lambda _: contextlib.nullcontext()):
        out = graphs.euler_reference(model, 10, cache, None, None, None, torch.ones(1,50,32))
    assert len(model.calls) == 10
    np.testing.assert_allclose(out, -1., atol=1e-6)
    assert out.dtype == torch.float32


def test_absolute_quantile_actions_and_masked_arm_follow_training(tmp_path):
    cls = implementation()
    import json
    import torch
    from rc_qwen35_history.configuration import load_model_config
    model_file(tmp_path)
    path = tmp_path/'weights/train_config.json'
    train = json.loads(path.read_text())
    train['dataset'].update(action_mode='abs', norm_mode='q01_q99',
                           pad_keys=['states.left_joint.position','actions.left_joint.position'])
    path.write_text(json.dumps(train))
    backend = cls(load_model_config('model.json', project_root=tmp_path))
    backend.prepare_transforms()
    result = backend.finalize_actions(torch.zeros(1,50,32), np.full(14,100,np.float32))
    np.testing.assert_allclose(result[:,:6],0)
    np.testing.assert_allclose(result[:,6:],1.,atol=1e-6)
    from test_qwen35_history_runtime import frames
    sample = backend.prepare_sample(np.full(14,2,np.float32), frames(),'task')
    np.testing.assert_array_equal(sample['observation.state'][:6],0)


def test_real_expert_projections_stay_fp32_with_bf16_backbone():
    implementation()
    from types import SimpleNamespace
    import torch
    from transformers.models.qwen3_5.configuration_qwen3_5 import Qwen3_5TextConfig
    from lerobot.policies.qwen3_5_context_video_ki import Qwen3_5ContextVideoKIConfig, Qwen3_5ContextVideoKIPolicy
    from lerobot.policies.qwen3_5_context_video_ki.modeling_qwen3_5_context_video_ki import Qwen3_5ContextVideoKIModel
    from lerobot.policies.qwen3_5_unified_cond.tokenization_qwen3_5_unified_cond import (
        IBQ_TOKEN_START, CONTROL_TOKEN_START, CONTROL_TOKEN_END, UnifiedSpecialTokenIds)
    class Backbone(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.embedding = torch.nn.Embedding(IBQ_TOKEN_START,8,dtype=torch.bfloat16)
            self.head = torch.nn.Linear(8,IBQ_TOKEN_START,bias=False,dtype=torch.bfloat16)
            self.head.weight = self.embedding.weight
            text = Qwen3_5TextConfig(hidden_size=8,intermediate_size=16,num_hidden_layers=1,
                     num_attention_heads=1,num_key_value_heads=1,head_dim=8,layer_types=['full_attention'])
            self.config = SimpleNamespace(text_config=text,tie_word_embeddings=True)
        def get_input_embeddings(self): return self.embedding
        def get_output_embeddings(self): return self.head
    config = Qwen3_5ContextVideoKIConfig(device='cpu',action_expert_hidden_size=8,
                                        action_expert_intermediate_size=16,gradient_checkpointing=False)
    tokens = UnifiedSpecialTokenIds(*range(CONTROL_TOKEN_START,CONTROL_TOKEN_END))
    model = Qwen3_5ContextVideoKIModel(config,qwen=Backbone(),special_tokens=tokens)
    policy = Qwen3_5ContextVideoKIPolicy(config,model=model).to(device='cpu')
    assert next(model.qwen.parameters()).dtype == torch.bfloat16
    assert next(model.action_expert.parameters()).dtype == torch.bfloat16
    for name in policy._FP32_ACTION_PATH_MODULES:
        assert getattr(model,name).weight.dtype == torch.float32
    embeds, mask, blocks = model.embed_suffix(torch.zeros(1,32),torch.ones(1,50,32),torch.ones(1))
    assert embeds.shape == (1,51,8) and embeds.dtype == torch.float32
    assert mask.all() and blocks[0,:3].tolist() == [1,1,0]


def test_processor_receives_owned_history_and_all_current_views(tmp_path):
    cls = implementation()
    from rc_qwen35_history.configuration import load_model_config
    from rc_qwen35_history.history import HistoryFrame
    from test_qwen35_history_runtime import frames
    model_file(tmp_path)
    backend = cls(load_model_config('model.json',project_root=tmp_path))
    backend.prepare_transforms()
    from lerobot.policies.qwen3_5_context_video_ki.transform_qwen3_5_context_video_ki import Qwen3_5ContextVideoKIProcessorTransformFn
    processor = Qwen3_5ContextVideoKIProcessorTransformFn(context_meta_position='last')
    sample = backend.prepare_sample(np.zeros(14,np.float32),frames(42),'task')
    assert len(processor._current_images(sample)) == 3
    class Sink:
        def build_inference(self, data, **kwargs): return data, kwargs
    backend.processor = Sink()
    hist = HistoryFrame(10, np.full((3,144,192),17,np.uint8))
    data, kwargs = backend.build_input(np.zeros(14),frames(),'task',(hist,))
    content, videos, metadata, markers = processor._clip_content(kwargs['contexts'])
    assert len(videos)==1 and videos[0].shape==(1,3,144,192)
    assert metadata[0].frames_indices == [10] and metadata[0].fps == 30
    assert kwargs['inference_order']=='action_first' and kwargs['generate']==()
    hist.image[:] = 255
    assert float(videos[0].max()) == pytest.approx(17/255)


@pytest.mark.parametrize('difference', [.01, .06, float('nan')])
def test_graph_warmup_accepts_only_finite_reference_agreement(tmp_path, monkeypatch, difference):
    cls = implementation()
    import torch
    from types import SimpleNamespace
    from rc_qwen35_history.configuration import load_model_config
    from rc_qwen35_history import graphs
    model_file(tmp_path)
    backend = cls(load_model_config('model.json',project_root=tmp_path))
    backend.prepare_transforms()
    backend.device = torch.device('cpu')
    backend.model_config = SimpleNamespace(num_inference_steps=10,max_prompt_tokens=850)
    backend.model = SimpleNamespace(sample_noise=lambda shape,device: torch.zeros(shape,device=device))
    monkeypatch.setattr(backend,'build_input',lambda q,imgs,task,contexts: {'depth':len(contexts)})
    monkeypatch.setattr(backend,'eager_prefix',lambda data: (None,torch.ones(1,data['depth']+5),None,None))
    monkeypatch.setattr(graphs,'euler_reference',lambda *args: torch.zeros(1,50,32))
    class Sampler:
        def __init__(self,*args,**kwargs): self.noise=torch.zeros(1,50,32)
        def load(self,*args): return True
        def capture(self): pass
        def replay(self): return torch.full((1,50,32),difference)
        def reference(self,*args): return torch.zeros(1,50,32)
    monkeypatch.setattr(graphs,'FlowSampler',Sampler)
    # Simulate unsupported prefix kernels: denoise still remains available.
    def unsupported(*args,**kwargs): raise RuntimeError('unsupported prefix kernel')
    monkeypatch.setattr(graphs,'capture_prefix_graphs',unsupported)
    backend.warmup()
    assert (backend.sampler is not None) == (difference==.01)
    assert backend.prefix_graph is None
    from rc_policy_sdk.monitor import normalize_snapshot
    snapshot = normalize_snapshot(backend.monitor())
    assert snapshot['model_stage'] == 'idle'
    assert snapshot['denoise_graph_ready'] == int(difference==.01)
    assert snapshot['prefix_graph_layouts'] == 0 and snapshot['graph_error']


def test_checkpoint_loader_accepts_deferred_device_move(tmp_path):
    implementation()
    import torch
    from safetensors.torch import save_model
    from lerobot.policies.pretrained import PreTrainedPolicy
    source = torch.nn.Linear(3,2)
    source.weight.data.fill_(.125)
    path = tmp_path/'tiny.safetensors'
    save_model(source,str(path))
    target = torch.nn.Linear(3,2)
    restored = PreTrainedPolicy._load_as_safetensor(target,str(path),None,True)
    torch.testing.assert_close(restored.weight,source.weight)
    assert restored.weight.device.type == 'cpu'


@pytest.mark.parametrize('path', ['eager', 'denoise_graph', 'prefix_graph'])
def test_predict_monitor_reports_actual_stage_path_and_prefix_tokens(tmp_path, monkeypatch, path):
    cls = implementation()
    import torch
    from types import SimpleNamespace
    from rc_qwen35_history.configuration import load_model_config
    from rc_qwen35_history import graphs
    from rc_policy_sdk.monitor import normalize_snapshot
    model_file(tmp_path)
    backend = cls(load_model_config('model.json', project_root=tmp_path))
    backend.prepare_transforms()
    backend.device = torch.device('cpu')
    backend.model_config = SimpleNamespace(num_inference_steps=10)
    backend.model = SimpleNamespace(sample_noise=lambda shape, device: torch.zeros(shape))
    monkeypatch.setattr(backend, 'build_input', lambda *args: {})
    def prefix(data):
        assert backend.monitor()['model_stage'] == 'prefix'
        return None, torch.ones(1, 17), None, None
    monkeypatch.setattr(backend, 'eager_prefix', prefix)
    def denoise(*args):
        assert backend.monitor()['model_stage'] == 'denoise'
        return torch.zeros(1, 50, 32)
    monkeypatch.setattr(graphs, 'euler_reference', denoise)
    monkeypatch.setattr(torch.cuda, 'synchronize', lambda *args: None)
    if path != 'eager':
        backend.sampler = SimpleNamespace(noise=torch.zeros(1,50,32), sample=denoise, replay=denoise)
    if path == 'prefix_graph':
        backend.prefix_graph = SimpleNamespace(prepare=lambda _: {'prefix_len': 23}, run=lambda _: True)
    actions, timings = backend.predict(np.zeros(14, np.float32), {}, 'task', ())
    np.testing.assert_array_equal(actions, np.ones((50,14), np.float32))
    assert set(timings) == {'processor_ms', 'prefix_ms', 'denoise_ms', 'post_ms'}
    snapshot = normalize_snapshot(backend.monitor())
    assert snapshot['model_stage'] == 'idle'
    assert snapshot['prefix_tokens'] == (23 if path == 'prefix_graph' else 17)
    assert snapshot['prediction_path'] == {'eager': 'eager', 'denoise_graph': 'eager_prefix+denoise_graph',
                                           'prefix_graph': 'prefix_graph+denoise_graph'}[path]


def test_prefix_graph_rejects_nonfinite_reference_difference(monkeypatch):
    implementation()
    import torch
    from types import SimpleNamespace
    from rc_qwen35_history import graphs
    class Prefix:
        def __init__(self,*args): self.graphs={}
        def prepare(self,row): return {'key':((),()),'prefix_len':1,'anchor':torch.zeros(1)}
        def capture(self,row): self.graphs[row['key']]=object()
        def run(self,row): return True
        def drop(self,key): self.graphs.pop(key,None)
    class Sampler:
        max_prefix_len=2
        full_layers=()
        noise=torch.zeros(1)
        calls=0
        def load(self,*args): return True
        def replay(self):
            self.calls+=1
            return torch.zeros(1) if self.calls==1 else torch.tensor([float('nan')])
    monkeypatch.setattr(graphs,'PrefixGraph',Prefix)
    monkeypatch.setattr(torch.cuda,'synchronize',lambda: None)
    result = graphs.capture_prefix_graphs(None,Sampler(),[{}],
        eager_prefix=lambda row:(None,torch.ones(1,1),torch.zeros(1),None))
    assert result is None
