"""Run with the production Qwen dependency image and read-only source/patch mounts."""
import argparse
import hashlib
import importlib
from importlib import metadata
import json
from pathlib import Path
import sys
import sysconfig

from rc_policy_sdk.source import validate_sources

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--require-cuda', action='store_true')
parser.add_argument('--policy', choices=('qwen35_single', 'qwen35_twostage'), default='qwen35_single')
args = parser.parse_args()
module_name = 'rc_qwen35_twostage' if args.policy == 'qwen35_twostage' else 'rc_qwen35_policy'
folder = Path('/workspace/policy') / args.policy
check = importlib.import_module(module_name + '.patches').check

# Check before importing any model modules.
patched = check()
sources = validate_sources({
    "rc_policy_sdk": "/workspace/packages/policy-sdk/src",
    module_name: str(folder / 'src'),
    "lerobot": str(folder / 'vendor'),
})
import torch
import transformers
import rc_policy_sdk
import lerobot
from lerobot.dataset_schemas import get_schema
from lerobot.policies.qwen3_5vla_ki_wan import Qwen3_5VLAKIWanConfig, Qwen3_5VLAKIWanPolicy


assert sys.version_info[:2] == (3, 11)
assert not sysconfig.get_config_var('Py_GIL_DISABLED')
expected = {'torch': '2.10.0+cu128', 'torchvision': '0.25.0+cu128',
            'transformers': '5.2.0', 'numpy': '2.2.6', 'posix-ipc': '1.3.2'}
for name, version in expected.items():
    assert metadata.version(name) == version, (name, metadata.version(name))
assert torch.version.cuda == '12.8'
site = Path(sysconfig.get_path('purelib')).resolve()
for module in (transformers,):
    assert Path(module.__file__).resolve().is_relative_to(site), module.__file__
schema = get_schema('arx_acone')
assert len(schema.image_mapping) == 3 and schema.action_mask_spec == [6, -1, 6, -1]
assert Qwen3_5VLAKIWanPolicy.config_class is Qwen3_5VLAKIWanConfig

# Report current mounted files; the historical vendor manifest is provenance,
# not a freeze on edits to the mounted implementation.
vendor = folder / 'vendor'
files = {str(path.relative_to(vendor)): hashlib.sha256(path.read_bytes()).hexdigest()
         for path in vendor.rglob('*') if path.is_file() and path.suffix in ('.py', '.yaml', '.yml')}
assert files
available = torch.cuda.is_available()
if args.require_cuda:
    assert available, 'CUDA is not visible inside the container'
print(json.dumps({'python': sys.version.split()[0], 'versions': expected,
                  'cuda_build': torch.version.cuda, 'cuda_available': available,
                  'policy': args.policy, 'sources': sources, 'vendor_file_hashes': files,
                  'schema': 'arx_acone', 'patch_verified': True}))
