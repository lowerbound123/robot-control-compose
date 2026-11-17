"""Strict, local LeRobot Pi05 inference inside the policy's own environment."""
import copy
import gc
from importlib import metadata
from pathlib import Path
import sys

import numpy as np

from .configuration import CAMERAS, processor_step_key, read_processor

LEROBOT_VERSION = '0.6.1'


def load_local_policy(checkpoint, policy_config):
    """Load a complete native checkpoint without PI05's exception-swallowing override.

    The base loader uses safetensors.load_model, including shared-tensor handling,
    and propagates malformed files and strict state-dictionary failures.
    """
    from lerobot.policies.pi05.modeling_pi05 import PI05Policy
    from lerobot.policies.pretrained import PreTrainedPolicy

    return PreTrainedPolicy.from_pretrained.__func__(
        PI05Policy, Path(checkpoint), config=policy_config, local_files_only=True, strict=True)


def _mode(value):
    return getattr(value, 'value', value)


def validate_normalization(pipeline, feature_name, feature, expected_mode, processor_name):
    steps = [s for s in pipeline.steps if getattr(s.__class__, '_registry_name', '') == processor_name]
    if len(steps) != 1:
        raise ValueError(f'{feature_name}: expected one {processor_name}')
    step = steps[0]
    stored = step.features.get(feature_name)
    if stored is None or stored.type != feature.type or tuple(stored.shape) != tuple(feature.shape):
        raise ValueError(f'{feature_name}: normalization feature differs from checkpoint')
    actual_mode = _mode(step.norm_map.get(feature.type, 'IDENTITY'))
    if actual_mode != _mode(expected_mode):
        raise ValueError(f'{feature_name}: normalization mode {actual_mode} differs from checkpoint {expected_mode}')
    selected = step.normalize_observation_keys
    if (processor_name == 'normalizer_processor' and actual_mode != 'IDENTITY'
            and selected is not None and feature_name not in selected):
        raise ValueError(f'{feature_name}: excluded by saved normalize_observation_keys')
    keys = {'IDENTITY': (), 'MEAN_STD': ('mean', 'std'), 'MIN_MAX': ('min', 'max'),
            'QUANTILES': ('q01', 'q99')}.get(actual_mode)
    if keys is None:
        raise ValueError(f'{feature_name}: unsupported normalization mode {actual_mode}')
    stats = step.state_dict()
    arrays = {}
    for key in keys:
        value = stats.get(f'{feature_name}.{key}')
        if value is None:
            raise ValueError(f'{feature_name}: missing saved normalization statistic {key}')
        array = value.detach().float().cpu().numpy()
        shapes = {(14,)} if _mode(feature.type) != 'VISUAL' else {(3,), (3, 1, 1)}
        if array.shape not in shapes or not np.isfinite(array).all():
            raise ValueError(f'{feature_name}: invalid normalization statistic {key}')
        arrays[key] = array
    if ('std' in arrays and (arrays['std'] < 0).any()
            or 'q01' in arrays and (arrays['q99'] < arrays['q01']).any()
            or 'min' in arrays and (arrays['max'] < arrays['min']).any()):
        raise ValueError(f'{feature_name}: invalid normalization statistic range')


class Pi05Backend:
    def __init__(self, config):
        self.config = copy.deepcopy(config)
        self.checkpoint = Path(config['model']['checkpoint'])
        self.chunk_size = config['execution']['chunk_size']
        self.model_horizon = config['model']['chunk_size']
        self.device = config['model']['device']
        self.scale = np.asarray(config['observations']['state_scale'], dtype=np.float32)
        self.offset = np.asarray(config['observations']['state_offset'], dtype=np.float32)
        self.policy = self.policy_config = self.preprocessor = self.postprocessor = None
        self._closed = False

    def _processor_overrides(self, data, tokenizer=None):
        overrides = {}
        for step in data['steps']:
            key, kind = processor_step_key(step)
            if kind == 'device_processor':
                overrides[key] = {'device': self.device if tokenizer is not None else 'cpu'}
            elif kind in ('normalizer_processor', 'unnormalizer_processor'):
                # Tokenization runs before moving the batch to the GPU. The
                # native normalizer moves statistics to its input device itself.
                overrides[key] = {'device': 'cpu'}
            elif kind == 'tokenizer_processor' and tokenizer is not None:
                overrides[key] = {'tokenizer_name': self.config['model']['tokenizer_path'],
                                  'tokenizer': tokenizer}
        return overrides

    def load(self):
        import torch
        from transformers import AutoTokenizer
        from lerobot.configs import PreTrainedConfig
        from lerobot.policies.pi05.configuration_pi05 import PI05Config
        # Registers the Pi05-specific saved tokenizer preparation step.
        from lerobot.policies.pi05 import processor_pi05  # noqa: F401
        from lerobot.policies.factory import make_pre_post_processors

        if self._closed or self.policy is not None:
            raise RuntimeError('Pi05 backend is closed or already loaded')
        if metadata.version('lerobot') != LEROBOT_VERSION:
            raise RuntimeError(f'Pi05 image requires lerobot=={LEROBOT_VERSION}; rebuild the dedicated image')
        if self.device.startswith('cuda') and not torch.cuda.is_available():
            raise RuntimeError('Pi05 CUDA device is unavailable; check NVIDIA runtime/GPU selection')
        config = PreTrainedConfig.from_pretrained(self.checkpoint, local_files_only=True)
        if not isinstance(config, PI05Config):
            raise ValueError('checkpoint is not a native LeRobot pi05 policy')
        config.device = self.device
        config.compile_model = self.config['model']['compile_model']
        if config.chunk_size != self.model_horizon or config.n_action_steps != self.chunk_size:
            raise ValueError('checkpoint horizon changed after preflight')
        tokenizer = AutoTokenizer.from_pretrained(self.config['model']['tokenizer_path'],
                                                  local_files_only=True, trust_remote_code=False)
        model = self.config['model']
        pre = read_processor(self.checkpoint, model['preprocessor_filename'])
        post = read_processor(self.checkpoint, model['postprocessor_filename'])
        self.preprocessor, self.postprocessor = make_pre_post_processors(
            config, pretrained_path=str(self.checkpoint),
            preprocessor_config_filename=model['preprocessor_filename'],
            postprocessor_config_filename=model['postprocessor_filename'],
            preprocessor_overrides=self._processor_overrides(pre, tokenizer),
            postprocessor_overrides=self._processor_overrides(post))
        for name in ('observation.state', *self.config['observations']['camera_map'].values()):
            feature = config.input_features[name]
            validate_normalization(self.preprocessor, name, feature,
                                   config.normalization_mapping.get(_mode(feature.type), 'IDENTITY'),
                                   'normalizer_processor')
        validate_normalization(self.postprocessor, 'action', config.output_features['action'],
                               config.normalization_mapping.get('ACTION', 'IDENTITY'), 'unnormalizer_processor')
        self.policy_config = config
        self.policy = load_local_policy(self.checkpoint, config)
        self.policy.requires_grad_(False)
        self.policy.eval()

    def _inputs(self, qpos, images, instruction):
        import torch

        qpos = np.asarray(qpos, dtype=np.float32)
        if qpos.shape != (14,) or not np.isfinite(qpos).all():
            raise ValueError('qpos must contain 14 finite values')
        with np.errstate(over='ignore', invalid='ignore'):
            native_state = qpos * self.scale + self.offset
        if not np.isfinite(native_state).all():
            raise ValueError('affine state overflow: model observation must be finite')
        sample = {'observation.state': torch.from_numpy(native_state.copy()),
                  'task': instruction}
        for camera in CAMERAS:
            frame = np.asarray(images[camera])
            if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8 or not frame.size:
                raise ValueError(f'{camera} must be nonempty RGB uint8 HWC')
            sample[self.config['observations']['camera_map'][camera]] = (
                torch.from_numpy(frame.copy()).permute(2, 0, 1).to(torch.float32) / 255.0)
        return sample

    def predict(self, qpos, images, instruction):
        import torch

        if self._closed or self.policy is None:
            raise RuntimeError('Pi05 backend is not loaded')
        with torch.inference_mode():
            batch = self.preprocessor(self._inputs(qpos, images, instruction))
            prediction = self.policy.predict_action_chunk(batch)
            shape = (1, self.model_horizon, 14)
            if (not isinstance(prediction, torch.Tensor) or tuple(prediction.shape) != shape
                    or not torch.isfinite(prediction).all().item()):
                raise ValueError(f'Pi05 must return finite model actions shaped {shape}')
            prediction = self.postprocessor(prediction)
            if not isinstance(prediction, torch.Tensor) or tuple(prediction.shape) != shape:
                raise ValueError(f'Pi05 postprocessor must preserve action shape {shape}')
            actions = prediction[0].float().cpu().numpy().copy()
        if self.config['actions']['representation'] == 'delta_joint':
            anchor = np.asarray(qpos, dtype=np.float32) * self.scale + self.offset
            anchor[[6, 13]] = 0
            actions += anchor
        actions = (actions - self.offset) / self.scale
        if not np.isfinite(actions).all():
            raise ValueError('Pi05 postprocessed actions are nonfinite')
        return actions[:self.chunk_size].astype(np.float32, copy=True)

    def warmup(self):
        if self.policy is None:
            raise RuntimeError('Pi05 backend is not loaded')
        height, width = self.policy_config.image_resolution
        frames = {camera: np.zeros((height, width, 3), np.uint8) for camera in CAMERAS}
        for _ in range(self.config['model']['warmup_runs']):
            self.predict(np.zeros(14, np.float32), frames, self.config['runtime']['instruction'])
        if self.device.startswith('cuda'):
            sys.modules['torch'].cuda.synchronize(self.device)
        self.reset_context()

    def reset_context(self):
        for component in (self.policy, self.preprocessor, self.postprocessor):
            if component is not None:
                component.reset()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.policy = self.policy_config = self.preprocessor = self.postprocessor = None
        gc.collect()
        torch = sys.modules.get('torch')
        if torch is not None and torch.cuda.is_initialized():
            torch.cuda.empty_cache()
