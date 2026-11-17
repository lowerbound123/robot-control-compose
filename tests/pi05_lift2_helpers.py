"""Metadata from the dev export; small, hand-checkable statistics, no model weights."""
import json
from pathlib import Path

from pi05_helpers import write_json

FIXTURES = Path(__file__).parent / 'fixtures/pi05_lift2'
CAMERAS = {'head': 'observation.images.image0', 'left_wrist': 'observation.images.image1',
           'right_wrist': 'observation.images.image2'}


def checkpoint_fixture(root, horizon=50, execute=None):
    checkpoint, tokenizer = root / 'weights', root / 'tokenizer'
    checkpoint.mkdir()
    tokenizer.mkdir()
    cfg = json.loads((FIXTURES / 'config.json').read_text())
    cfg.update(chunk_size=horizon, n_action_steps=execute or horizon, dtype='float32')
    train = json.loads((FIXTURES / 'train_config.json').read_text())
    write_json(checkpoint / 'config.json', cfg)
    write_json(checkpoint / 'train_config.json', train)
    (checkpoint / 'model.safetensors').write_bytes(b'metadata-only-test')
    stats = {}
    for group in ('states', 'actions'):
        for part, size in (('left_joint', 6), ('left_gripper', 1), ('right_joint', 6), ('right_gripper', 1)):
            stats[f'{group}.{part}.position'] = {
                'mean': [1. if group == 'states' else 10.] * size,
                'std': [2.] * size,
            }
    write_json(checkpoint / 'stats.json', {'arx_lift2': stats, 'arx_acone': {}})
    write_json(tokenizer / 'tokenizer_config.json', {})
    write_json(tokenizer / 'tokenizer.json', {})
    raw = {'model': {'checkpoint': 'weights', 'tokenizer_path': 'tokenizer',
                     'device': 'cpu', 'warmup_runs': 0},
           'runtime': {'instruction': 'pick up the object'},
           'observations': {'camera_map': dict(CAMERAS)},
           'actions': {'representation': 'delta_joint'}, 'execution': {}}
    path = root / 'model.json'
    write_json(path, raw)
    return path, raw, cfg, train, stats


def local_tokenizer(path):
    from tokenizers import Tokenizer
    from tokenizers.models import WordLevel
    from tokenizers.pre_tokenizers import Whitespace
    from transformers import PreTrainedTokenizerFast

    tokenizer = Tokenizer(WordLevel({'<pad>': 0, '<unk>': 1, 'Task': 2, 'Action': 3,
                                    'State': 4, '128': 5, '170': 6, '0': 7, '255': 8}, unk_token='<unk>'))
    tokenizer.pre_tokenizer = Whitespace()
    PreTrainedTokenizerFast(tokenizer_object=tokenizer, pad_token='<pad>', unk_token='<unk>').save_pretrained(path)
