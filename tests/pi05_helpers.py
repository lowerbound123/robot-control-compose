"""Small checkpoint metadata fixtures; the byte placeholders are not model weights."""
import json
from pathlib import Path

CAMERAS = {'head': 'observation.images.head', 'left_wrist': 'observation.images.left_wrist',
           'right_wrist': 'observation.images.right_wrist'}


def write_json(path, value):
    Path(path).write_text(json.dumps(value))


def checkpoint_fixture(root, horizon=50, execute=None):
    checkpoint = root / 'weights'
    tokenizer = root / 'tokenizer'
    checkpoint.mkdir()
    tokenizer.mkdir()
    features = {'observation.state': {'type': 'STATE', 'shape': [14]},
                **{name: {'type': 'VISUAL', 'shape': [3, 224, 224]} for name in CAMERAS.values()}}
    cfg = {'type': 'pi05', 'input_features': features,
           'output_features': {'action': {'type': 'ACTION', 'shape': [14]}},
           'chunk_size': horizon, 'n_action_steps': execute or horizon,
           'max_state_dim': 32, 'max_action_dim': 32, 'empty_cameras': 0,
           'normalization_mapping': {'STATE': 'QUANTILES', 'ACTION': 'QUANTILES', 'VISUAL': 'IDENTITY'}}
    write_json(checkpoint / 'config.json', cfg)
    (checkpoint / 'model.safetensors').write_bytes(b'metadata-test-only')
    norm_config = {'features': {**features, **cfg['output_features']},
                   'norm_map': cfg['normalization_mapping']}
    pre = {'steps': [
        {'registry_name': 'rename_observations_processor', 'config': {'rename_map': {}}},
        {'registry_name': 'to_batch_processor', 'config': {}},
        {'registry_name': 'normalizer_processor', 'config': norm_config, 'state_file': 'pre_stats.safetensors'},
        {'registry_name': 'pi05_prepare_state_tokenizer_processor_step', 'config': {'max_state_dim': 32}},
        {'registry_name': 'tokenizer_processor', 'config': {'tokenizer_name': 'original/tokenizer'}},
        {'registry_name': 'device_processor', 'config': {'device': 'cuda'}},
    ]}
    post = {'steps': [
        {'registry_name': 'unnormalizer_processor', 'config': norm_config, 'state_file': 'post_stats.safetensors'},
        {'registry_name': 'device_processor', 'config': {'device': 'cpu'}},
    ]}
    write_json(checkpoint / 'policy_preprocessor.json', pre)
    write_json(checkpoint / 'policy_postprocessor.json', post)
    for name in ('pre_stats.safetensors', 'post_stats.safetensors'):
        (checkpoint / name).write_bytes(b'metadata-test-only')
    write_json(tokenizer / 'tokenizer_config.json', {})
    write_json(tokenizer / 'tokenizer.json', {})
    model = {'model': {'checkpoint': 'weights', 'tokenizer_path': 'tokenizer'},
             'runtime': {'instruction': 'pick up the object'},
             'observations': {'camera_map': CAMERAS},
             'actions': {'representation': 'absolute'}, 'execution': {}}
    path = root / 'model.json'
    write_json(path, model)
    return path, model, cfg, pre, post


def native_checkpoint_fixture(root, monkeypatch, *, relative=False, normalization='MEAN_STD'):
    """Serialize real LeRobot processors using a tiny local text tokenizer."""
    import torch
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from transformers import PreTrainedTokenizerFast
    from lerobot.configs import NormalizationMode
    from lerobot.policies.pi05.configuration_pi05 import PI05Config
    from lerobot.policies.pi05 import processor_pi05
    from lerobot.processor import TokenizerProcessorStep

    path, model, *_ = checkpoint_fixture(root, horizon=5, execute=3)
    tokenizer = Tokenizer(WordLevel({'<pad>': 0, '<unk>': 1, 'Task': 2, 'Action': 3}, unk_token='<unk>'))
    tokenizer.pre_tokenizer = Whitespace()
    PreTrainedTokenizerFast(tokenizer_object=tokenizer, pad_token='<pad>', unk_token='<unk>').save_pretrained(root / 'tokenizer')
    cfg = PI05Config.from_pretrained(root / 'weights', local_files_only=True)
    cfg.device = 'cpu'
    cfg.dtype = 'float32'
    cfg.num_inference_steps = 2
    cfg.tokenizer_max_length = 24
    cfg.use_relative_actions = relative
    cfg.action_feature_names = ([f'left_joint_{i}' for i in range(6)] + ['left_gripper']
                                + [f'right_joint_{i}' for i in range(6)] + ['right_gripper'])
    cfg.normalization_mapping = {'VISUAL': NormalizationMode.IDENTITY,
                                 'STATE': NormalizationMode(normalization), 'ACTION': NormalizationMode(normalization)}
    cfg.save_pretrained(root / 'weights')
    stats = {'observation.state': {'mean': torch.ones(14), 'std': torch.full((14,), 2.),
                                    'q01': torch.full((14,), -1.), 'q99': torch.full((14,), 3.)},
             'action': {'mean': torch.full((14,), 10.), 'std': torch.full((14,), 2.),
                        'q01': torch.full((14,), 8.), 'q99': torch.full((14,), 12.)}}
    with monkeypatch.context() as patch:
        patch.setattr(processor_pi05, 'TokenizerProcessorStep',
                      lambda **kw: TokenizerProcessorStep(**{**kw, 'tokenizer_name': str(root / 'tokenizer')}))
        pre, post = processor_pi05.make_pi05_pre_post_processors(cfg, dataset_stats=stats)
    pre.save_pretrained(root / 'weights')
    post.save_pretrained(root / 'weights')
    # Loading must replace the training-machine/Hub tokenizer reference with our
    # configured local tokenizer. No network access should be necessary.
    pre_path = root / 'weights/policy_preprocessor.json'
    saved = json.loads(pre_path.read_text())
    for step in saved['steps']:
        if step.get('registry_name') == 'tokenizer_processor':
            step['config']['tokenizer_name'] = 'not-a-local-tokenizer/do-not-fetch'
    write_json(pre_path, saved)
    model['model'].update(device='cpu', warmup_runs=0)
    write_json(path, model)
    return path, model, cfg


def tiny_pi05_architecture(monkeypatch):
    """Shrink layer widths only; keep official model operators and sampling."""
    import torch
    from transformers import PaliGemmaConfig, GemmaConfig
    from lerobot.policies.pi05 import modeling_pi05 as module

    monkeypatch.setattr(module, 'get_gemma_config', lambda variant: module.GemmaConfig(
        width=32, depth=1, mlp_dim=64, num_heads=2, num_kv_heads=1, head_dim=16))

    def paligemma():
        return PaliGemmaConfig(
            vision_config={'hidden_size': 32, 'intermediate_size': 64, 'num_hidden_layers': 1,
                           'num_attention_heads': 2, 'image_size': 224, 'patch_size': 112, 'projection_dim': 32},
            text_config={'hidden_size': 32, 'intermediate_size': 64, 'num_hidden_layers': 1,
                         'num_attention_heads': 2, 'num_key_value_heads': 1, 'head_dim': 16, 'vocab_size': 257152})
    monkeypatch.setattr(module, 'CONFIG_MAPPING', {'paligemma': paligemma, 'gemma': GemmaConfig})
    original = module.PaliGemmaWithExpertModel.__init__

    def small_projector(self, *args, **kwargs):
        original(self, *args, **kwargs)
        projector = self.paligemma.model.multi_modal_projector
        projector.linear = torch.nn.Linear(projector.linear.in_features, 32)
    monkeypatch.setattr(module.PaliGemmaWithExpertModel, '__init__', small_projector)
