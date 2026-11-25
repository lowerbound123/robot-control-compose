"""Materialize an A2 inference model from its complete safetensors file."""
import torch
from huggingface_hub.constants import SAFETENSORS_SINGLE_FILE
from safetensors import safe_open
from transformers import AutoModelForImageTextToText, Qwen3_5Config
from transformers.models.qwen3_5.modeling_qwen3_5 import (
    Qwen3_5TextRotaryEmbedding, Qwen3_5VisionRotaryEmbedding,
)

_INPUT_TABLE = 'model.qwen.model.language_model.embed_tokens.weight'
_OUTPUT_TABLE = 'model.qwen.lm_head.weight'


def _build_meta_policy(policy_type, config):
    from lerobot.policies.qwen3_5_context_video_ki.modeling_qwen3_5_context_video_ki import (
        Qwen3_5Tokenizer, ensure_unified_special_tokens, resolve_local_snapshot,
    )

    base = resolve_local_snapshot(config.vlm_model_name_or_path)
    tokenizer = Qwen3_5Tokenizer.from_pretrained(base, local_files_only=True)
    tokens = ensure_unified_special_tokens(tokenizer)
    qwen_config = Qwen3_5Config.from_pretrained(base, local_files_only=True)
    with torch.device('meta'):
        qwen = AutoModelForImageTextToText.from_config(
            qwen_config, dtype=getattr(torch, config.dtype), attn_implementation=config.attn_implementation)
        return policy_type(config, qwen=qwen, tokenizer=tokenizer, special_tokens=tokens)


def _restore_rotary_buffers(policy, device):
    # These frequency tables are intentionally absent from safetensors.
    for name, module in tuple(policy.named_modules()):
        if isinstance(module, Qwen3_5VisionRotaryEmbedding):
            rotary = Qwen3_5VisionRotaryEmbedding(module.dim, module.theta)
        elif isinstance(module, Qwen3_5TextRotaryEmbedding):
            rotary = Qwen3_5TextRotaryEmbedding(module.config, device='cpu')
        else:
            continue
        for key, buffer in module.named_buffers(recurse=False):
            setattr(rotary, key, getattr(rotary, key).to(device=device, dtype=buffer.dtype))
        policy.set_submodule(name, rotary)


def _materialize(weights, expected, device):
    names = set(weights.keys())
    stored, alias = ((_OUTPUT_TABLE, _INPUT_TABLE) if _OUTPUT_TABLE in names
                     else (_INPUT_TABLE, _OUTPUT_TABLE))
    required = expected.keys() - {alias}
    missing, extra = sorted(required - names), sorted(names - required)
    if missing or extra:
        raise RuntimeError(f'A2 checkpoint keys differ from the model: missing={missing}, unexpected={extra}')
    for key in required:
        shape = tuple(weights.get_slice(key).get_shape())
        if shape != tuple(expected[key].shape):
            raise RuntimeError(f'A2 checkpoint shape mismatch for {key}: {shape} != {tuple(expected[key].shape)}')
    # Match the destination dtypes of the original copy-based loader. Each
    # stored tensor is read and transferred once, including the tied table.
    state = {key: weights.get_tensor(key).to(device=device, dtype=expected[key].dtype) for key in weights.keys()}
    state[alias] = state[stored]
    return state


def load_single_checkpoint(checkpoint, policy_type, config, device):
    with safe_open(checkpoint / SAFETENSORS_SINGLE_FILE, framework='pt', device='cpu') as weights:
        names = set(weights.keys())
        if {_INPUT_TABLE, _OUTPUT_TABLE} <= names:
            # Older manually saved files may carry different values under
            # both names. Let the original loader apply its assignment order.
            return None
        policy = _build_meta_policy(policy_type, config)
        _restore_rotary_buffers(policy, device)
        expected = policy.state_dict()
        if any(buffer.is_meta and name not in expected for name, buffer in policy.named_buffers()):
            return None
        policy.load_state_dict(_materialize(weights, expected, device), strict=True, assign=True)
    # assign=True installs separate Parameters even for shared storage.
    qwen = policy.model.qwen
    qwen.get_output_embeddings().weight = qwen.get_input_embeddings().weight
    return policy
