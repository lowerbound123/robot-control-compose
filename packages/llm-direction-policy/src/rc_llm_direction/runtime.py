"""Synchronous worker callback; only RC owns action execution and frame leases."""
import base64
import io
import json
import time
import uuid

import numpy as np
from PIL import Image

from .actions import epos, make_chunk
from .configuration import validate_config
from .errors import ProviderError

CAMERAS = ('head', 'left_wrist', 'right_wrist')


def encode_images(images, params):
    encoded = {}
    for name in CAMERAS:
        if name not in images:
            raise ProviderError('缺少三路图像中的相机：' + name)
        frame = np.asarray(images[name])
        if (frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3
                or min(frame.shape[:2]) < 1):
            raise ProviderError('相机图像必须是非空 RGB uint8：' + name)
        # Own pixels only inside this callback. Neither the provider nor context gets mmap views.
        with Image.fromarray(frame.copy()) as picture:
            picture.thumbnail((params['image_max_side'], params['image_max_side']), Image.Resampling.LANCZOS)
            with io.BytesIO() as stream:
                picture.save(stream, format='JPEG', quality=params['jpeg_quality'])
                encoded[name] = 'data:image/jpeg;base64,' + base64.b64encode(stream.getvalue()).decode('ascii')
    if sum(len(value) for value in encoded.values()) > 16 * 1024 * 1024:
        raise ProviderError('编码图像超过 16 MiB；请减小 image_max_side 或 jpeg_quality')
    return encoded


class DirectionPolicy:
    def __init__(self, config, *, provider=None):
        self.params = validate_config(config)
        if provider is None:
            from .providers import ProviderClient
            provider = ProviderClient(self.params['provider'])
        self.provider = provider
        self.closed = False

    def policy(self, state, action_chunk, images, runtime):
        if self.closed:
            raise RuntimeError('llm_direction client is closed')
        with np.errstate(over='ignore', invalid='ignore'):
            remaining = np.array(action_chunk, dtype=np.float32, copy=True)
        if remaining.shape == (0,):
            remaining = remaining.reshape(0, 14)
        if remaining.ndim != 2 or remaining.shape[1] != 14 or not np.isfinite(remaining).all():
            raise ValueError('action_chunk must contain finite (T,14) epos targets')
        if len(remaining) > self.params['request_when_remaining']:
            return remaining
        anchor = remaining[-1].copy() if len(remaining) else epos(state['eef'])
        request_id, started = uuid.uuid4().hex, time.monotonic()
        metadata = {'provider': self.params['provider']['type'], 'model': self.params['provider']['model'],
                    'request_id': request_id}
        try:
            encoded = encode_images(images, self.params)
            decision = self.provider.decide(self.params['task'], self.params['prompt'], encoded)
            metadata.update(left=decision.left, right=decision.right, done=decision.done)
            if decision.done:
                runtime.pause('llm_direction：LLM 判断任务已完成')
                return anchor[None, :]
            return np.concatenate((remaining, make_chunk(anchor, decision, self.params)), axis=0)
        except ProviderError as exc:
            metadata['error'] = str(exc)
            runtime.pause('llm_direction：' + str(exc))
            return anchor[None, :]
        finally:
            metadata['elapsed_s'] = round(time.monotonic() - started, 3)
            print(json.dumps(metadata, ensure_ascii=False, allow_nan=False), flush=True)

    def reset_context(self):
        # Each decision uses current images and config; there is no conversation or action cache.
        pass

    def close(self):
        if not self.closed:
            self.closed = True
            self.provider.close()
