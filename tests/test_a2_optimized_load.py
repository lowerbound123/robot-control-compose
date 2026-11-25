"""A2 checkpoint loading uses the real model with a small hidden size."""
import importlib

import pytest

from test_a2_optimized import entrypoint
from test_qwen35_history_backend import isolated_vendor


@pytest.fixture
def checkpoint(tmp_path, monkeypatch):
    torch = pytest.importorskip('torch')
    from safetensors.torch import save_model
    from transformers import AutoModelForImageTextToText, Qwen3_5Config
    from lerobot.policies.qwen3_5_context_video_ki import modeling_qwen3_5_context_video_ki as model

    vlm = tmp_path / 'vlm'
    qwen_config = Qwen3_5Config(
        tie_word_embeddings=True,
        text_config=dict(vocab_size=model.IBQ_TOKEN_START, hidden_size=8, intermediate_size=16,
                         num_hidden_layers=2, num_attention_heads=1, num_key_value_heads=1,
                         head_dim=8, layer_types=['linear_attention', 'full_attention'],
                         linear_num_key_heads=1, linear_num_value_heads=1, linear_key_head_dim=32,
                         linear_value_head_dim=32),
        vision_config=dict(depth=1, hidden_size=8, intermediate_size=16, num_heads=2,
                           out_hidden_size=8, num_position_embeddings=16),
    )
    qwen_config.save_pretrained(vlm)
    config = model.Qwen3_5ContextVideoKIConfig(
        dtype='bfloat16', device='cpu', vlm_model_name_or_path=str(vlm),
        attn_implementation='sdpa', gradient_checkpointing=False,
        action_expert_hidden_size=8, action_expert_intermediate_size=16,
    )
    config.device = None
    tokenizer = object()
    tokens = model.UnifiedSpecialTokenIds(*range(model.CONTROL_TOKEN_START, model.CONTROL_TOKEN_END))
    monkeypatch.setattr(model.Qwen3_5Tokenizer, 'from_pretrained', lambda *a, **kw: tokenizer)
    monkeypatch.setattr(model, 'ensure_unified_special_tokens', lambda t: tokens)
    backbone = AutoModelForImageTextToText.from_config(qwen_config, dtype=torch.bfloat16, attn_implementation='sdpa')
    backbone.save_pretrained(vlm)
    policy_type = model.Qwen3_5ContextVideoKIPolicy
    save_model(policy_type(config, qwen=backbone, tokenizer=tokenizer, special_tokens=tokens),
               tmp_path / 'model.safetensors')
    reference = policy_type.from_pretrained(tmp_path, config=config, local_files_only=True)
    for path in vlm.glob('*.safetensors'):
        path.unlink()
    entrypoint()
    backend_type = importlib.import_module('_a2_test.backend').OptimizedBackend
    backend = backend_type({'model': {'checkpoint': str(tmp_path)}})
    backend.device = torch.device('cpu')
    return backend, config, policy_type, reference


@pytest.mark.usefixtures('isolated_vendor')
@pytest.mark.parametrize('device', ['cpu', 'cuda'])
def test_loads_once_without_base_weights_and_matches_actions(checkpoint, device):
    import torch
    from rc_qwen35_history.graphs import euler_reference

    backend, config, policy_type, reference = checkpoint
    if device == 'cuda' and not torch.cuda.is_available():
        pytest.skip('requires CUDA')
    backend.device = torch.device(device)
    actual = backend._load_policy(policy_type, config).to(device=backend.device).eval()
    reference.to(device=backend.device)
    assert backend.monitor()['checkpoint_load_path'] == 'single_checkpoint'
    # The base directory contains a config and deliberately has no weights.
    assert not list(backend.checkpoint.joinpath('vlm').glob('*.safetensors'))
    assert actual.model.qwen.get_input_embeddings().weight is actual.model.qwen.get_output_embeddings().weight
    for name, value in reference.state_dict().items():
        torch.testing.assert_close(actual.state_dict()[name], value, rtol=0, atol=0)
    for name, value in reference.named_buffers():
        torch.testing.assert_close(actual.get_buffer(name), value, rtol=0, atol=0)

    noise = torch.randn((1, config.chunk_size, config.max_action_dim), generator=torch.Generator().manual_seed(7)).to(device)
    with torch.no_grad():
        outputs = []
        for policy in (reference, actual):
            inputs = torch.tensor([[1, 2]], device=device)
            kv = policy.model.qwen.model.language_model(input_ids=inputs, use_cache=True).past_key_values
            outputs.append(euler_reference(policy.model, 2, kv, torch.ones_like(inputs, dtype=torch.bool),
                                           torch.zeros((4, 1, 1), dtype=torch.long, device=device),
                                           torch.zeros((1, config.max_state_dim), device=device), noise))
    assert outputs[0].isfinite().all()
    torch.testing.assert_close(*outputs, rtol=0, atol=0)


@pytest.mark.usefixtures('isolated_vendor')
def test_single_checkpoint_rejects_incomplete_state(checkpoint, tmp_path):
    from safetensors.torch import load_file, save_file

    backend, config, policy_type, _ = checkpoint
    weights = load_file(backend.checkpoint / 'model.safetensors')
    del weights['model.action_out_proj.weight']
    broken = tmp_path / 'broken'
    broken.mkdir()
    save_file(weights, broken / 'model.safetensors')
    backend.checkpoint = broken
    with pytest.raises(RuntimeError, match='model.action_out_proj.weight'):
        backend._load_policy(policy_type, config)


@pytest.mark.usefixtures('isolated_vendor')
def test_checkpoint_with_both_saved_tables_uses_legacy_path(checkpoint, tmp_path, monkeypatch):
    from safetensors.torch import load_file, save_file

    backend, config, policy_type, reference = checkpoint
    weights = load_file(backend.checkpoint / 'model.safetensors')
    for name in ('model.qwen.model.language_model.embed_tokens.weight', 'model.qwen.lm_head.weight'):
        weights[name] = reference.state_dict()[name].clone()
    legacy = tmp_path / 'legacy'
    legacy.mkdir()
    save_file(weights, legacy / 'model.safetensors')
    backend.checkpoint = legacy
    calls = []

    def load_old(path, **kwargs):
        calls.append((path, kwargs))
        return reference

    monkeypatch.setattr(policy_type, 'from_pretrained', load_old)
    assert backend._load_policy(policy_type, config) is reference
    assert calls == [(legacy, {'config': config, 'local_files_only': True})]
    assert backend.monitor()['checkpoint_load_path'] == 'legacy'
