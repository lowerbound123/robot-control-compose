"""Real, reduced Pi05 operators and strict safetensors loading (no trained weights)."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

torch = pytest.importorskip('torch')


def implementation():
    assert importlib.util.find_spec('rc_pi05_lift2.modeling') is not None, 'independent dev model runtime missing'
    from rc_pi05_lift2 import modeling
    return modeling


def config(dtype='float32'):
    data = json.loads((Path(__file__).parent/'fixtures/pi05_lift2/config.json').read_text())
    data.update(dtype=dtype, chunk_size=3, n_action_steps=3, num_inference_steps=2,
                device='cpu', compile_model=False)
    return SimpleNamespace(**data)


def shrink(module, monkeypatch):
    from transformers import GemmaConfig, PaliGemmaConfig
    monkeypatch.setattr(module, 'get_gemma_config', lambda _: module.GemmaConfig(
        width=32, depth=1, mlp_dim=64, num_heads=2, num_kv_heads=1, head_dim=16))
    def paligemma():
        return PaliGemmaConfig(vision_config={'hidden_size':32, 'intermediate_size':64,
            'num_hidden_layers':1, 'num_attention_heads':2, 'image_size':224, 'patch_size':112},
            text_config={'vocab_size':257152})
    monkeypatch.setattr(module, 'CONFIG_MAPPING', {'paligemma':paligemma,'gemma':GemmaConfig})
    original = module.PaliGemmaWithExpertModel.__init__
    def small_projector(self,*args,**kwargs):
        original(self,*args,**kwargs)
        self.paligemma.model.multi_modal_projector.linear = torch.nn.Linear(32,32)
    monkeypatch.setattr(module.PaliGemmaWithExpertModel,'__init__',small_projector)


def inputs():
    return ([torch.linspace(-1,1,3*224*224).reshape(1,3,224,224)]*3,
            [torch.ones(1,dtype=torch.bool)]*3,
            torch.tensor([[2,3,4,0]]),torch.tensor([[True,True,True,False]]))


def save_weights(model, path, mutation=None):
    from safetensors.torch import save_file
    state = {k:v.detach().contiguous().clone() for k,v in model.state_dict().items()}
    tied = 'model.paligemma_with_expert.paligemma.model.language_model.embed_tokens.weight'
    state.pop(tied)
    if mutation == 'missing': state.pop('model.action_in_proj.weight')
    elif mutation == 'unexpected': state['model.unexpected'] = torch.ones(1)
    elif mutation == 'shape': state['model.action_in_proj.weight'] = torch.ones(1,1)
    elif mutation == 'collision': state['action_in_proj.weight'] = state['model.action_in_proj.weight'].clone()
    elif mutation == 'alias': state[tied] = state['model.paligemma_with_expert.paligemma.lm_head.weight'] + 1
    save_file(state,str(path))


def test_runtime_rejects_unpatched_transformers(tmp_path, monkeypatch):
    module = implementation()
    import transformers
    monkeypatch.setattr(transformers,'__file__',str(tmp_path/'__init__.py'))
    with pytest.raises(RuntimeError,match='patch|补丁|runtime'):
        module.verify_runtime()


def test_complete_checkpoint_restores_tied_embeddings_and_same_predictions(tmp_path, monkeypatch):
    module = implementation()
    module.verify_runtime()
    shrink(module,monkeypatch)
    torch.manual_seed(42)
    model = module.Pi05Model(config()).eval()
    path = tmp_path/'model.safetensors'
    save_weights(model,path)
    loaded = module.Pi05Model(config()).eval()
    module.load_weights(loaded,path)
    noise = torch.linspace(-.7,.7,96).reshape(1,3,32)
    with torch.inference_mode():
        expected = model.model.sample_actions(*inputs(),noise=noise)
        result = loaded.model.sample_actions(*inputs(),noise=noise)
        repeated = loaded.model.sample_actions(*inputs(),noise=noise)
    assert result.shape == (1,3,32) and torch.isfinite(result).all()
    torch.testing.assert_close(result,expected,rtol=0,atol=0)
    torch.testing.assert_close(repeated,result,rtol=0,atol=0)
    assert not torch.equal(result,noise)


@pytest.mark.parametrize('mutation',['missing','unexpected','shape','collision','alias'])
def test_incomplete_or_ambiguous_weights_never_fall_back_to_random_initialization(tmp_path, monkeypatch, mutation):
    module = implementation()
    shrink(module,monkeypatch)
    model = module.Pi05Model(config())
    path = tmp_path/'model.safetensors'
    save_weights(model,path,mutation)
    with pytest.raises((RuntimeError,ValueError)):
        module.load_weights(model,path)


def test_prefix_uses_raw_vision_embeddings_and_only_language_sqrt_scaling(monkeypatch):
    module = implementation()
    shrink(module,monkeypatch)
    model = module.Pi05Model(config())
    expert = model.model.paligemma_with_expert
    monkeypatch.setattr(expert,'embed_image',lambda image:torch.ones(1,2,32))
    monkeypatch.setattr(expert,'embed_language_tokens',lambda tokens:torch.ones(1,4,32))
    prefix,mask,attention = model.model.embed_prefix(*inputs())
    torch.testing.assert_close(prefix[:,:6],torch.ones(1,6,32))
    torch.testing.assert_close(prefix[:,6:],torch.full((1,4,32),np.sqrt(32)))
    assert mask.tolist()==[[True]*9+[False]]
    assert not attention.any()


def test_fixed_noise_matches_the_running_dev_reference(monkeypatch):
    module = implementation()
    shrink(module,monkeypatch)
    model = module.Pi05Model(config()).eval()
    # Values are synthetic and independent of constructor RNG/order. The golden
    # was evaluated in the known dev runtime, not by the implementation here.
    with torch.no_grad():
        for name,p in model.model.named_parameters():
            factor = (sum(name.encode())%7+1)*.002
            values = (torch.arange(p.numel(),dtype=torch.float32).remainder(23)-11)*factor
            p.copy_(values.reshape(p.shape))
    golden = json.loads((Path(__file__).parent/'fixtures/pi05_lift2/reference_output.json').read_text())
    noise = torch.linspace(-.7,.7,96).reshape(1,3,32)
    with torch.inference_mode():
        prefix,_,_ = model.model.embed_prefix(*inputs())
        actions = model.model.sample_actions(*inputs(),noise=noise)
    torch.testing.assert_close(prefix,torch.tensor(golden['prefix']),rtol=1e-5,atol=1e-6)
    torch.testing.assert_close(actions,torch.tensor(golden['actions']),rtol=1e-5,atol=1e-6)


def test_model_boundary_converts_rgb_range_once(monkeypatch):
    module = implementation()
    shrink(module,monkeypatch)
    model = module.Pi05Model(config()).eval()
    batch = {'observation.language.tokens':torch.tensor([[1]]),
             'observation.language.attention_mask':torch.ones(1,1,dtype=torch.bool)}
    for i, value in enumerate([0.,.5,1.]):
        batch[f'observation.images.image{i}'] = torch.full((1,3,224,224),value)
        batch[f'observation.images.image{i}_mask'] = torch.ones(1,dtype=torch.bool)
    def infer(images,masks,tokens,attention):
        for image,want in zip(images,[-1.,0.,1.]):
            torch.testing.assert_close(image,torch.full_like(image,want))
        return torch.zeros(1,3,32)
    monkeypatch.setattr(model.model,'sample_actions',infer)
    assert model.predict_action_chunk(batch).shape == (1,3,32)


@pytest.mark.parametrize('dtype',['float32','bfloat16'])
def test_offline_probe_loads_real_tiny_weights_tokenizes_and_runs_inference(tmp_path,monkeypatch,capsys,dtype):
    from pi05_lift2_helpers import checkpoint_fixture,local_tokenizer
    from pi05_helpers import write_json
    from rc_pi05_lift2.configuration import load_model_config
    from rc_pi05_lift2.probe import main
    module = implementation()
    shrink(module,monkeypatch)
    path,_,metadata,_,_ = checkpoint_fixture(tmp_path,horizon=3,execute=2)
    metadata['dtype']=dtype
    write_json(tmp_path/'weights/config.json',metadata)
    local_tokenizer(tmp_path/'tokenizer')
    settings = load_model_config(path,project_root=tmp_path)
    model = module.Pi05Model(SimpleNamespace(**settings['checkpoint_config']))
    save_weights(model,tmp_path/'weights/model.safetensors')
    del model
    assert main(['--config',str(path)]) == 0
    report = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert report['status']=='ready' and report['action_shape']==[2,14]
    assert report['synthetic_observations'] is True


def test_bfloat16_preserves_reference_fp32_weights(monkeypatch):
    module = implementation()
    shrink(module,monkeypatch)
    model = module.Pi05Model(config('bfloat16'))
    params = dict(model.named_parameters())
    assert params['model.action_in_proj.weight'].dtype == torch.float32
    assert params['model.time_mlp_in.weight'].dtype == torch.float32
    for name,p in model.model.paligemma_with_expert.named_parameters():
        if 'input_layernorm' in name or 'post_attention_layernorm' in name or 'model.norm' in name:
            assert p.dtype == torch.float32, name
    assert params['model.paligemma_with_expert.gemma_expert.model.layers.0.self_attn.q_proj.weight'].dtype == torch.bfloat16


def test_architecture_matches_actual_pi05_zero_weight_shapes():
    module = implementation()
    # No allocation for the 3.7B parameter architecture.
    with torch.device('meta'):
        model = module.Pi05Model(config('bfloat16'))
    expected = json.loads((Path(__file__).parent/'fixtures/pi05_lift2/weight_shapes.json').read_text())
    actual = {k:list(v.shape) for k,v in model.state_dict().items()}
    assert actual == expected
