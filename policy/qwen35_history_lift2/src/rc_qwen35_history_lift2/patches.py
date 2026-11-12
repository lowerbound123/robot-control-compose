"""Verify the isolated, read-only Transformers source mount before model imports."""
import hashlib
from importlib import metadata, util
from pathlib import Path


def check():
    if metadata.version('transformers') != '5.2.0':
        raise RuntimeError('history policy requires transformers==5.2.0')
    spec = util.find_spec('transformers')
    target = Path(spec.origin).parent / 'models/qwen3_5/modeling_qwen3_5.py'
    source_spec = util.find_spec('lerobot')
    source = Path(source_spec.origin).parent / 'policies/qwen3_5vla/transformers_replace/models/qwen3_5/modeling_qwen3_5.py'
    if hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest():
        raise RuntimeError('history Transformers patch differs; recreate this policy container with its source mount')
    return target
