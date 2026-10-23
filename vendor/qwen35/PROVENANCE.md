# Qwen3.5 inference source provenance

This directory is a local, installable subset of `postrain4internvla` at commit
`617449fbbf0ca2a821ba25e1545c651037250c86`, copied on 2026-10-21.
The source checkout used for extraction was `/Volumes/Coding/projects/postrain4internvla`.
The installed runtime does not import or require that checkout, deploydev, or model-server.
`SOURCE_MANIFEST.json` records the source path and SHA-256 of every copied/adapted source file,
and the hash of its resulting vendored contents.

The adapter in `packages/qwen35-policy/src/rc_qwen35_policy/backend.py` derives from
`deploydev/models/qwen35.py` and `deploydev/models/common.py` at the same commit.
It supports the deployed `Qwen3_5VLAKIWanPolicy` action path for `single` and `two_stage`.
The model's optimized KI and KI/Wan implementations and action token code are copied
without numerical changes. Both workflows return canonical 50×14 actions before RC's
post-fusion gripper operation.

## Local adaptations

- Minimal package initialization replaces global registration of unrelated policies.
- Checkpoint dataclass fields, schema registry, ACOne schema, resize, normalization,
  image remapping, chat processor and model weight loading preserve the source behavior.
- Training dataset config/transform imports, other policy wrappers, model self-tests,
  training upload helpers and WAN video model implementation are excluded. Optimizer
  presets and policy training forward raise `NotImplementedError`.
- The public KI/Wan wrapper accepts only the optimized action backend; it retains
  checkpoint naming, action expert float32 casting, state padding and action slicing.
- Helpers are copied as a bounded source subset. Dataset type hints are postponed;
  they do not require dataset implementations to be installed.
- The adapter drops HTTP/RTC paths, uses a dictionary configuration, validates shapes
  and finite outputs, clones the delta anchor to prevent NumPy observation mutation,
  and obtains JSON statistics without importing dataset utilities.
- The two-stage generation stops on the first FAST token and omits that token, caching
  the generated subtask for three predictions by default. Reset and release clear it.
- `scripts/install_qwen35_patches.py` discovers the active interpreter's Transformers
  directory, requires exactly 5.2.0, backs up the original file once and verifies the hash.
  The source patch is `src/lerobot/policies/qwen3_5vla/transformers_replace/models/qwen3_5/modeling_qwen3_5.py`,
  the exact Qwen3.5 replacement selected by source `tutorials/1.installation.md`.
  The similarly named KI/FAST replacements are not substituted.

## Licenses and notices

`LICENSE` is an exact copy of the source repository's root license: proprietary,
Copyright (c) 2026 Shanghai AI Laboratory. Its authorized-personnel restrictions apply;
this vendor directory does not grant additional distribution or use rights.

Individual source files also contain their original Apache-2.0 notices from
Hugging Face, Physical Intelligence and/or the Qwen team. Those notices are preserved.
`LICENSE-APACHE-2.0` supplies the Apache-2.0 text for those components. See each source
file for its applicable notice; this directory must not be described as wholly
Apache-licensed. No checkpoint/model weights are included.

## Environment and validation limits

The deployment target is Linux NVIDIA CUDA 12.8, Python 3.11, torch 2.10.0,
torchvision 0.25.0, Transformers 5.2.0, and Draccus 0.10.x. This Docker migration
builds the shared model image with `docker/qwen35.Dockerfile` and
`requirements/qwen35.lock`; dependencies and the patch are installed at image build
time. It does not use Conda. Do not install a separate upstream `lerobot` into this
environment because this distribution owns the same Python namespace.

The policy checkpoint directory must already contain `config.json`, `stats.json`,
and its safetensors weights. The configured Qwen base model and processor/tokenizer
assets must also be available locally or in the Hugging Face cache. This package
contains source code, not those assets. Prepare the required local/cache assets
before deployment. The source chat processor uses `Qwen/Qwen3.5-2B`.
Optional `causal-conv1d` / `flash-linear-attention` kernels are not required by the
copied patch, which has a PyTorch fallback; GPU latency must be measured on the target.

CPU tests exercise real tensor preprocessing, source transforms, statistics,
config deserialization, output mapping, generation token trimming/cache cadence and
patch installation. Model generation is replaced only at its heavy boundary. No
checkpoint inference, CUDA graph execution, CUDA memory/latency measurement or
robot acceptance result is claimed by these tests.

## Docker migration provenance

Copied from the current `robot-control` workspace on 2026-10-23. All 25 source
files covered by `SOURCE_MANIFEST.json` retain their recorded vendored hashes;
licenses and the original source-commit identity remain unchanged. Container
packaging and deployment documentation are maintained by `robot-control-compose`.
The original checkout and the training repository are not runtime dependencies.
