"""Read-only validation of Qwen's directly mounted Transformers module."""
from __future__ import annotations

import hashlib
from importlib import metadata, util
from pathlib import Path

VERSION = '5.2.0'
SOURCE = (Path(__file__).resolve().parents[4] /
          'vendor/qwen35/lerobot/policies/qwen3_5vla/transformers_replace/models/qwen3_5/modeling_qwen3_5.py')


def check() -> Path:
    version = metadata.version('transformers')
    if version != VERSION:
        raise RuntimeError(f'Qwen3.5 patch requires transformers=={VERSION}, found {version}')
    spec = util.find_spec('transformers')
    if spec is None or spec.origin is None:
        raise RuntimeError('transformers package not found')
    destination = Path(spec.origin).parent / 'models/qwen3_5/modeling_qwen3_5.py'
    if not destination.is_file():
        raise FileNotFoundError(f'Expected Transformers Qwen3.5 module missing: {destination}')
    if hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(SOURCE.read_bytes()).digest():
        raise RuntimeError('Qwen3.5 Transformers patch is missing or differs; recreate the policy container with current source mounts')
    return destination
