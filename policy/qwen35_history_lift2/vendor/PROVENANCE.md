# Qwen3.5 context-video action inference subset

Extracted on 2026-11-06 from the user-designated checkout at
`10.6.3.69:/home/pjlab/caijh/qwen3_5_vqa/lerobot_lab`, commit
`314c3fcbcfeedc95b2899145dba5c34e4292eda4`.
`deploy/acone/infer_a2.py` was untracked there: its SHA-256 is
`90eb883e86a4a2d9840c52fecd90bdd0b8c5674314539c0c80464a9acbced321`.
The runtime never imports that checkout or the existing Qwen policy's vendor tree.

`SOURCE_MANIFEST.json` records source and resulting hashes. Existing repository
inference-only checkpoint loaders, schema and small utilities are independently
snapshotted where recorded as `vendor/qwen35/...`; their earlier origin remains
documented in that directory. They are not runtime dependencies of this copy.

## Adaptations

- Preserve checkpoint dataclass fields, token/marker IDs, prompt construction,
  resize/normalization/reorder numerics, image/video embedding, state projections,
  prefix cache, action expert and Euler inference.
- Exclude training datasets/optimizers, loss computation, language generation,
  FAST decoding and IBQ model implementation. Retain IBQ token geometry and
  vocabulary layout because existing checkpoint weights and prompt headers need
  them even on the action-first route. Unsupported execution paths raise errors.
- Extract only the required normalization/resize/reorder transforms. Parse the
  saved dataset statistics and processor parameters without TrainPipelineConfig.
- Snapshot minimal package initializers to avoid registering unrelated policies.
- Import `packaging.version` explicitly in the checkpoint loader, avoiding an
  import-order dependency when validating the safetensors version. Deferred
  device moves retain the source behavior.
- `rc_qwen35_history/graphs.py` extracts the denoise and prefix graph helpers from
  infer_a2.py; robot control, ROS, input(), image dumps and the original scheduler
  are replaced by the project's existing callback/lifecycle boundary.
- Reject nonfinite prefix graph/reference differences, as well as differences
  above the original 0.05 threshold.
- The new Qwen3.5 Transformers patch is mounted read-only and checked against
  Transformers 5.2.0 before model import. Three delta-rule fallback zero buffers
  allocate directly on the input device, preserving dtype and recurrence while
  avoiding host allocation during CUDA graph capture. This does not alter the
  patch used by the two existing Qwen policies.

## Licenses and assets

LICENSE preserves the source repository license (Shanghai AI Laboratory
proprietary terms); original per-file Apache notices and LICENSE-APACHE-2.0 are
retained. This subset grants no additional rights and is not wholly Apache-licensed.
No weights, tokenizer assets, camera frames or training data are included.

Target: independent Python 3.11 Linux container, Torch2.10+CUDA12.8,
torchvision0.25, Transformers5.2.0. Dependencies are pinned and hashed in the
policy directory. Optional FLA/causal-conv kernels are not required; latency and
CUDA graph support require separate target-GPU validation.


This snapshot is owned by `policy/qwen35_history_lift2/vendor`. Historical `source` paths in SOURCE_MANIFEST.json record the original snapshot provenance; they are not runtime imports or mounts. Derived implementation paths point to this policy's own `src/rc_qwen35_history_lift2`.
