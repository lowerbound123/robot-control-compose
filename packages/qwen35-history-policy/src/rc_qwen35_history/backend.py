"""Action-first context-video inference, with no robot or training runtime."""
import dataclasses
import logging
from pathlib import Path
import time
from types import SimpleNamespace

import numpy as np

from .configuration import PARTS, WIDTHS, checkpoint_assets

LOG = logging.getLogger(__name__)


class HistoryBackend:
    def __init__(self, config):
        self.config = config
        self.checkpoint = Path(config['model']['checkpoint'])
        self.policy = self.model = self.processor = None
        self.sampler = self.prefix_graph = None
        self.device = None
        self._closed = False

    def prepare_transforms(self):
        import torch
        from lerobot.dataset_schemas import get_schema
        from lerobot.transforms.core import (NormalizeTransformFn, UnNormalizeTransformFn,
            RemapImageKeyTransformFn, ResizeImagesWithPadFn, ResizeImagesToLongSideFn,
            ReorderStateActionTransform, compose)
        from lerobot.transforms.utils import resize_to_long_side, resize_with_pad

        raw, self.dataset, self.stats = checkpoint_assets(self.checkpoint)
        self.geometry = SimpleNamespace(**raw)
        self.chunk_size = raw['chunk_size']
        self.history_slot_count = raw['context_slot_count']
        self.history_slot_stride = raw['context_slot_stride']
        self.schema = get_schema('arx_acone')
        self.norm_mode = self.dataset.get('norm_mode', 'mean_std')
        self.action_mode = self.dataset['action_mode']
        mapping = self.schema.image_mapping
        if raw['image_resize_mode'] == 'long_side':
            resize = ResizeImagesToLongSideFn(long_side=raw['image_resize_long_side'], mapping=mapping)
            self.current_resize = lambda image: resize_to_long_side(image, raw['image_resize_long_side'], resize.mode)
        else:
            height, width = raw.get('image_resolution', (256, 256))
            resize = ResizeImagesWithPadFn(height=height, width=width, mapping=mapping)
            self.current_resize = lambda image: resize_with_pad(image, height, width, resize.mode)
        side = raw.get('context_resize_long_side')
        self.clip_resize = self.current_resize if side is None else lambda image: resize_to_long_side(image, side, resize.mode)
        self.transforms = compose([resize, RemapImageKeyTransformFn(mapping=mapping),
            NormalizeTransformFn(selected_keys=['observation.state'], mode=self.norm_mode,
                                 norm_stats={'observation.state': self.stats['states']})])
        self.reorder = ReorderStateActionTransform(state_reorder=self.schema.state_reorder)
        self.unnormalize = UnNormalizeTransformFn(selected_keys=['action'], mode=self.norm_mode,
                                                  norm_stats={'action': self.stats['actions']})
        pad_keys = self.dataset.get('pad_keys') or []
        self.pad_masks = {prefix: torch.tensor([key in pad_keys for part, width in zip(PARTS, WIDTHS)
                          for key in [f'{prefix}.{part}.position']*width], dtype=torch.bool)
                          for prefix in ('states', 'actions')}

    @staticmethod
    def _image(image):
        import torch
        value = np.asarray(image)
        if value.dtype != np.uint8 or value.ndim != 3 or value.shape[2] != 3 or not value.size:
            raise ValueError('expected RGB uint8 HWC image')
        # Never construct a Tensor sharing a borrowed/read-only mmap.
        return torch.from_numpy(np.array(value, copy=True, order='C')).permute(2, 0, 1).float()/255.

    def resize_history(self, image):
        import torch
        return (self.clip_resize(self._image(image))*255.).round().clamp(0,255).to(torch.uint8).numpy().copy()

    def prepare_sample(self, qpos, images, instruction):
        import torch
        import torch.nn.functional as F
        qpos = np.asarray(qpos, dtype=np.float32)
        if qpos.shape != (14,) or not np.isfinite(qpos).all():
            raise ValueError('qpos must be finite and shaped (14,)')
        sample = {'observation.state': torch.from_numpy(qpos.copy()), 'task': instruction}
        for camera, key in [('head','head'), ('left_wrist','hand_left'), ('right_wrist','hand_right')]:
            sample[f'images.rgb.{key}'] = self._image(images[camera])
        sample = self.transforms(sample)
        state = sample['observation.state'].masked_fill(self.pad_masks['states'], 0.)
        sample['observation.state'] = F.pad(state, (0, self.geometry.max_state_dim-len(state)))
        return self.reorder(sample)

    def build_input(self, qpos, images, instruction, contexts):
        import torch
        from lerobot.policies.qwen3_5_context_video.tokenization_qwen3_5_context_video import ContextSlot, ContextSource
        slots = [ContextSlot(images=(torch.from_numpy(item.image.copy()).float()/255.,),
                             source=ContextSource.HIST, frame_index=item.index) for item in contexts]
        return self.processor.build_inference(self.prepare_sample(qpos, images, instruction),
            contexts=slots, include_task=True, inference_order='action_first', view_indices=(0,), language={}, generate=())

    def load(self):
        from .patches import check
        check()
        import torch
        from lerobot.configs.policies import PreTrainedConfig
        from lerobot.policies.qwen3_5_context_video_ki import Qwen3_5ContextVideoKIConfig, Qwen3_5ContextVideoKIPolicy
        from lerobot.policies.qwen3_5_context_video_ki.transform_qwen3_5_context_video_ki import Qwen3_5ContextVideoKIProcessorTransformFn
        if self._closed:
            raise RuntimeError('backend is closed')
        if not torch.cuda.is_available():
            raise RuntimeError('Qwen3.5 history requires a CUDA GPU')
        self.prepare_transforms()
        self.device = torch.device('cuda')
        torch.backends.cudnn.benchmark = True
        torch.backends.cuda.matmul.allow_tf32 = True
        config = PreTrainedConfig.from_pretrained(self.checkpoint, local_files_only=True)
        if not isinstance(config, Qwen3_5ContextVideoKIConfig):
            raise TypeError('wrong checkpoint policy type')
        config.device = None
        config.dtype = 'bfloat16'
        config.attn_implementation = 'sdpa'
        config.inference_backend = 'standard'
        config.inference_order = 'action_first'
        config.inference_action_type = 'fm'
        config.generate_subtask = False
        config.compile_model = False
        config.vlm_model_name_or_path = self.config['model']['vlm_model_name_or_path']
        if self.config['inference']['num_inference_steps'] is not None:
            config.num_inference_steps = self.config['inference']['num_inference_steps']
        self.policy = Qwen3_5ContextVideoKIPolicy.from_pretrained(self.checkpoint, config=config, local_files_only=True)
        self.policy.to(device=self.device).eval().requires_grad_(False)
        self.model, self.model_config = self.policy.model, config
        saved = self.dataset['data_transforms']['inputs'][-1]
        fields = {f.name for f in dataclasses.fields(Qwen3_5ContextVideoKIProcessorTransformFn) if f.init}
        unknown = set(saved) - fields - {'type'}
        if any(not key.startswith('_') for key in unknown):
            raise ValueError(f'unsupported saved processor fields: {sorted(unknown)}')
        args = {key: value for key, value in saved.items() if key in fields and not key.startswith('_')}
        args.update(vlm_model_name_or_path=config.vlm_model_name_or_path,
                    future_first_probability=0., inference_order='action_first', generate_subtask=False)
        self.processor = Qwen3_5ContextVideoKIProcessorTransformFn(**args)
        self.processor._ensure_loaded()

    def eager_prefix(self, model_input):
        import torch
        from .graphs import _inference_autocast
        batch = {}
        for key, value in model_input.items():
            if isinstance(value, str):
                batch[key] = [value]
            else:
                tensor = torch.as_tensor(value).unsqueeze(0).to(self.device)
                batch[key] = tensor.to(torch.bfloat16) if tensor.is_floating_point() else tensor
        with torch.no_grad(), _inference_autocast(self.model):
            _, kv, mask, _, anchor, _, _ = self.model._prefix_stage(batch, requested_order=0)
        return kv, mask, anchor, batch['observation.state'].float()

    def finalize_actions(self, prediction, qpos):
        import torch
        if tuple(prediction.shape) != (1, self.chunk_size, self.geometry.max_action_dim):
            raise ValueError('unexpected normalized chunk shape')
        normalized = prediction[0].float().cpu()
        restored = normalized.new_zeros((self.chunk_size, 14))
        for src_start, src_end, dst_start, dst_end in self.schema.action_reorder:
            restored[:, src_start:src_end] = normalized[:, dst_start:dst_end]
        actions = self.unnormalize({'action': restored})['action']
        if self.action_mode == 'delta':
            anchor = torch.from_numpy(np.array(qpos, dtype=np.float32, copy=True))
            actions = actions + torch.where(self.schema.action_mask, anchor, torch.zeros_like(anchor))
        home = actions.new_tensor([0,0,0,0,0,0,-3.4]*2)
        actions[:, self.pad_masks['actions']] = home[self.pad_masks['actions']]
        result = actions.detach().numpy().astype(np.float32, copy=True)
        if not np.isfinite(result).all():
            raise ValueError('nonfinite model actions')
        return result

    def predict(self, qpos, images, instruction, contexts):
        import torch
        from .graphs import euler_reference
        if self._closed or self.model is None:
            raise RuntimeError('backend is not loaded')
        timings = {}
        with torch.no_grad():
            started = time.perf_counter()
            model_input = self.build_input(qpos, images, instruction, contexts)
            timings['processor_ms'] = (time.perf_counter()-started)*1000
            started = time.perf_counter()
            graphed = self.prefix_graph is not None and self.prefix_graph.run(self.prefix_graph.prepare(model_input))
            if not graphed:
                kv, mask, anchor, state = self.eager_prefix(model_input)
            torch.cuda.synchronize(self.device)
            timings['prefix_ms'] = (time.perf_counter()-started)*1000
            started = time.perf_counter()
            if graphed:
                self.sampler.noise.normal_()
                normalized = self.sampler.replay()
            elif self.sampler is not None:
                normalized = self.sampler.sample(kv, mask, anchor, state)
            else:
                noise = self.model.sample_noise((1, self.chunk_size, self.geometry.max_action_dim), self.device)
                normalized = euler_reference(self.model, self.model_config.num_inference_steps, kv, mask, anchor, state, noise)
            torch.cuda.synchronize(self.device)
            timings['denoise_ms'] = (time.perf_counter()-started)*1000
            started = time.perf_counter()
            result = self.finalize_actions(normalized, qpos)
            timings['post_ms'] = (time.perf_counter()-started)*1000
        return result, timings

    def warmup(self):
        import torch
        from .graphs import FlowSampler, GRAPH_TOLERANCE, capture_prefix_graphs, euler_reference
        from .history import HistoryFrame
        qpos = self.stats['states'].get('mean')
        if qpos is None:
            qpos = (self.stats['states']['q01']+self.stats['states']['q99'])/2
        images = {name: np.zeros((480,640,3),np.uint8) for name in ('head','left_wrist','right_wrist')}
        frame = self.resize_history(images['head'])
        slots = tuple(HistoryFrame(i*self.history_slot_stride, frame) for i in range(self.history_slot_count))
        depths = (0, len(slots)) if self.config['history']['enabled'] else (0,)
        task = self.config['runtime']['instruction']
        longest = 0
        with torch.no_grad():
            for depth in depths:
                model_input = self.build_input(qpos, images, task, slots[:depth])
                for _ in range(2):
                    kv, mask, anchor, state = self.eager_prefix(model_input)
                    noise = self.model.sample_noise((1,self.chunk_size,self.geometry.max_action_dim),self.device)
                    euler_reference(self.model, self.model_config.num_inference_steps, kv, mask, anchor, state, noise)
                longest = max(longest, mask.shape[1])
            if not self.config['inference']['use_cuda_graph']:
                return
            candidate = None
            try:
                candidate = FlowSampler(self.model, num_steps=self.model_config.num_inference_steps,
                    max_prefix_len=max(longest, self.model_config.max_prompt_tokens)+64, attn_implementation='sdpa')
                if not candidate.load(kv, mask, anchor, state):
                    raise RuntimeError('warmup prefix exceeds graph buffers')
                candidate.capture()
                candidate.noise.normal_()
                noise = candidate.noise.clone()
                difference = float((candidate.replay()-candidate.reference(kv,mask,anchor,state,noise)).abs().max())
                if not np.isfinite(difference) or difference > GRAPH_TOLERANCE:
                    raise RuntimeError(f'denoise graph difference {difference} exceeds tolerance')
                self.sampler = candidate
            except Exception:
                LOG.exception('denoise CUDA graph unavailable; retaining eager inference')
            if self.sampler is not None and self.config['inference']['use_prefix_graph']:
                try:
                    self.prefix_graph = capture_prefix_graphs(self.model, self.sampler,
                        (self.build_input(qpos,images,task,slots[:depth]) for depth in
                         range(len(slots)+1 if self.config['history']['enabled'] else 1)), eager_prefix=self.eager_prefix)
                except Exception:
                    LOG.exception('prefix CUDA graphs unavailable; retaining eager prefix')
        LOG.info('history inference ready: denoise_graph=%s prefix_layouts=%d', self.sampler is not None,
                 0 if self.prefix_graph is None else len(self.prefix_graph.graphs))

    def reset_context(self):
        if self.policy is not None:
            self.policy.reset()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.prefix_graph = self.sampler = self.processor = self.model = self.policy = None
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
