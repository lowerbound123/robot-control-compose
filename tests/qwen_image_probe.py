"""Run in the production Qwen image without mounting the project/source tree."""
import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import sys
import sysconfig

import numpy as np
import torch
import transformers
import rc_policy_sdk
import rc_qwen35_policy
import lerobot
from lerobot.dataset_schemas import get_schema
from lerobot.policies.qwen3_5vla_ki_wan import Qwen3_5VLAKIWanConfig, Qwen3_5VLAKIWanPolicy


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--require-cuda', action='store_true')
args = parser.parse_args()
assert sys.version_info[:2] == (3, 11)
assert not sysconfig.get_config_var('Py_GIL_DISABLED')
expected = {'torch': '2.10.0+cu128', 'torchvision': '0.25.0+cu128',
            'transformers': '5.2.0', 'numpy': '2.2.6', 'posix-ipc': '1.3.2'}
for name, version in expected.items():
    assert metadata.version(name) == version, (name, metadata.version(name))
assert torch.version.cuda == '12.8'
site = Path(sysconfig.get_path('purelib')).resolve()
for module in (rc_policy_sdk, rc_qwen35_policy, lerobot, transformers):
    assert Path(module.__file__).resolve().is_relative_to(site), module.__file__
schema = get_schema('arx_acone')
assert len(schema.image_mapping) == 3 and schema.action_mask_spec == [6, -1, 6, -1]
assert Qwen3_5VLAKIWanPolicy.config_class is Qwen3_5VLAKIWanConfig

provenance = Path(sys.prefix) / 'share/rc-qwen35-vendor/SOURCE_MANIFEST.json'
manifest = json.loads(provenance.read_text())
for name, entry in manifest['files'].items():
    assert hashlib.sha256((site / name).read_bytes()).hexdigest() == entry['vendored_sha256'], name
patched = Path(transformers.__file__).parent / 'models/qwen3_5/modeling_qwen3_5.py'
source = site / 'lerobot/policies/qwen3_5vla/transformers_replace/models/qwen3_5/modeling_qwen3_5.py'
assert hashlib.sha256(patched.read_bytes()).digest() == hashlib.sha256(source.read_bytes()).digest()
available = torch.cuda.is_available()
if args.require_cuda:
    assert available, 'CUDA is not visible inside the container'
print(json.dumps({'python': sys.version.split()[0], 'versions': expected,
                  'cuda_build': torch.version.cuda, 'cuda_available': available,
                  'vendor_files_verified': len(manifest['files']), 'installed_wheels': True,
                  'schema': 'arx_acone', 'patch_verified': True}))
