"""Offline Pi05 dev-export inference; no training package or ROS imports."""
import copy
import gc
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np

from .configuration import CAMERAS, read_json


def load_local_policy(checkpoint, policy_config):
    from .modeling import Pi05Model, load_weights, verify_runtime

    verify_runtime()
    model = Pi05Model(policy_config)
    # The export is complete, including vision and language weights. Construct
    # the architecture directly, then require EVERY parameter to be loaded.
    # No redundant 12 GB base-model load or Hub fallback is necessary.
    load_weights(model, Path(checkpoint) / 'model.safetensors')
    return model.to(policy_config.device)


def state_prompt(state, instruction):
    """Training's mean/std state tokenizer, preserving contiguous joint order."""
    padded = np.pad(np.asarray(state, dtype=np.float32), (0, 18)) / 3
    bins = np.digitize(padded, np.linspace(-1, 1, 257)[:-1]) - 1
    task = instruction.strip().replace('_', ' ').replace('\n', ' ')
    return f'Task: {task}, State: {" ".join(map(str, bins))};\nAction: '


def resize_with_pad(image, size=224):
    import torch.nn.functional as F

    height, width = image.shape[-2:]
    if (height, width) == (size, size):
        return image
    scale = min(size / height, size / width)
    resized_height, resized_width = round(height * scale), round(width * scale)
    if min(resized_height, resized_width) < 1:
        raise ValueError('camera aspect ratio cannot be resized to 224 pixels')
    resized = F.interpolate(image[None], size=(resized_height, resized_width),
                            mode='bilinear', align_corners=False)[0]
    top, left = (size - resized_height) // 2, (size - resized_width) // 2
    return F.pad(resized, (left, size-resized_width-left, top, size-resized_height-top),
                 value=0.).clamp(0., 1.)


class Pi05Backend:
    def __init__(self, config, *, record_stage=None):
        self._record_stage = record_stage if record_stage is not None else lambda name: None
        self.config = copy.deepcopy(config)
        self.checkpoint = Path(config['model']['checkpoint'])
        self.chunk_size = config['execution']['chunk_size']
        self.model_horizon = config['model']['chunk_size']
        self.device = config['model']['device']
        self.scale = np.asarray(config['observations']['state_scale'], dtype=np.float32)
        self.offset = np.asarray(config['observations']['state_offset'], dtype=np.float32)
        self.policy = self.policy_config = self.tokenizer = None
        self.state_mean = self.state_std = self.action_mean = self.action_std = None
        self._closed = False

    def load(self):
        import torch
        from transformers import AutoTokenizer

        if self._closed or self.policy is not None:
            raise RuntimeError('Pi05 backend is closed or already loaded')
        if self.device.startswith('cuda') and not torch.cuda.is_available():
            raise RuntimeError('Pi05 CUDA device is unavailable; check NVIDIA runtime/GPU selection')
        metadata = read_json(self.checkpoint / 'config.json')
        if metadata != self.config['checkpoint_config']:
            raise ValueError('checkpoint configuration changed after preflight')
        cfg = SimpleNamespace(**metadata)
        cfg.device = self.device
        cfg.compile_model = self.config['model']['compile_model']
        cfg.compile_mode = metadata.get('compile_mode', 'reduce-overhead')
        self.dtype = getattr(torch, cfg.dtype)
        self.tokenizer = AutoTokenizer.from_pretrained(self.config['model']['tokenizer_path'],
                                                       local_files_only=True, trust_remote_code=False)
        for name in ('state', 'action'):
            for key in ('mean', 'std'):
                setattr(self, name+'_'+key, torch.tensor(self.config['statistics'][name][key],
                                                        dtype=torch.float32, device=self.device))
        # Match dev's selected bf16/fp32 weights; never cast the whole policy.
        self.policy_config = cfg
        self.policy = load_local_policy(self.checkpoint, cfg)
        self.policy.requires_grad_(False)
        self.policy.eval()
        if self.device.startswith('cuda'):
            torch.backends.cudnn.benchmark = True
            torch.backends.cuda.matmul.allow_tf32 = True

    def _native_state(self, qpos):
        qpos = np.asarray(qpos, dtype=np.float32)
        if qpos.shape != (14,) or not np.isfinite(qpos).all():
            raise ValueError('qpos must contain 14 finite values')
        with np.errstate(over='ignore', invalid='ignore'):
            state = qpos * self.scale + self.offset
        if not np.isfinite(state).all():
            raise ValueError('affine state overflow: model observation must be finite')
        return state

    def _inputs(self, native_state, images, instruction):
        import torch

        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError('instruction must be nonempty')
        state = torch.tensor(native_state, dtype=torch.float32, device=self.device)
        state = (state - self.state_mean) / (self.state_std + 1e-6)
        if not torch.isfinite(state).all().item():
            raise ValueError('normalized state must be finite')
        tokens = self.tokenizer(state_prompt(state.cpu().numpy(), instruction), max_length=200,
                                padding_side='right', padding='max_length', truncation=True)
        sample = {'observation.state': state[None].to(self.dtype), 'task': [instruction],
                  'observation.language.tokens': torch.tensor([tokens['input_ids']],
                                                              dtype=torch.int64, device=self.device),
                  'observation.language.attention_mask': torch.tensor([tokens['attention_mask']],
                                                                      dtype=torch.bool, device=self.device)}
        for camera in CAMERAS:
            frame = np.asarray(images[camera])
            if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8 or not frame.size:
                raise ValueError(f'{camera} must be nonempty RGB uint8 HWC')
            # Copy before constructing a tensor: shared image leases are readonly.
            image = torch.from_numpy(frame.copy()).to(device=self.device, dtype=self.dtype) / 255.
            target = self.config['observations']['camera_map'][camera]
            sample[target] = resize_with_pad(image.permute(2, 0, 1))[None]
            sample[target+'_mask'] = torch.ones(1, dtype=torch.bool, device=self.device)
        return sample

    def predict(self, qpos, images, instruction):
        import torch

        if self._closed or self.policy is None:
            raise RuntimeError('Pi05 backend is not loaded')
        self._record_stage('processor')
        native_state = self._native_state(qpos)
        with torch.inference_mode():
            batch = self._inputs(native_state, images, instruction)
            self._record_stage('model_forward')
            with torch.autocast(device_type=torch.device(self.device).type, dtype=self.dtype,
                                enabled=self.dtype != torch.float32):
                prediction = self.policy.predict_action_chunk(batch)
            shape = (1, self.model_horizon, 32)
            if (not isinstance(prediction, torch.Tensor) or tuple(prediction.shape) != shape
                    or not torch.isfinite(prediction).all().item()):
                raise ValueError(f'Pi05 must return finite model actions shaped {shape}')
            self._record_stage('post')
            # This export composes 14 contiguous channels then pads at the END.
            # A sparse 16-channel extraction would swap both grippers/joints.
            prediction = prediction[0, :, :14].float()
            actions = prediction * (self.action_std + 1e-6) + self.action_mean
            anchor = torch.tensor(native_state, device=actions.device, dtype=actions.dtype)
            anchor[[6, 13]] = 0
            actions = (actions + anchor).cpu().numpy()
        with np.errstate(over='ignore', invalid='ignore'):
            actions = (actions - self.offset) / self.scale
        if not np.isfinite(actions).all():
            raise ValueError('Pi05 postprocessed actions are nonfinite')
        return actions[:self.chunk_size].astype(np.float32, copy=True)

    def warmup(self):
        if self.policy is None:
            raise RuntimeError('Pi05 backend is not loaded')
        frames = {camera: np.zeros((224,224,3), np.uint8) for camera in CAMERAS}
        for _ in range(self.config['model']['warmup_runs']):
            self.predict(np.zeros(14, np.float32), frames, self.config['runtime']['instruction'])
        if self.device.startswith('cuda'):
            sys.modules['torch'].cuda.synchronize(self.device)
        self.reset_context()

    def reset_context(self):
        if self.policy is not None:
            self.policy.reset()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.policy = self.policy_config = self.tokenizer = None
        self.state_mean = self.state_std = self.action_mean = self.action_std = None
        gc.collect()
        torch = sys.modules.get('torch')
        if torch is not None and torch.cuda.is_initialized():
            torch.cuda.empty_cache()
