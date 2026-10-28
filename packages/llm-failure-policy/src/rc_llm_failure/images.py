"""Immutable, owned JPEG snapshots; no shared image views survive a callback."""
import base64
from dataclasses import dataclass
import io

import numpy as np
from PIL import Image

CAMERAS = ('head', 'left_wrist', 'right_wrist')
MAX_REQUEST_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class Snapshot:
    step: int
    images: tuple[tuple[str, str], ...]


def encode_snapshot(images, step, params):
    encoded = []
    size = 0
    for name in CAMERAS:
        if name not in images:
            raise ValueError('llm_failure 缺少相机：' + name)
        frame = np.asarray(images[name])
        if frame.dtype != np.uint8 or frame.ndim != 3 or frame.shape[2] != 3 or not frame.size:
            raise ValueError('相机图像必须是非空 RGB uint8：' + name)
        with Image.fromarray(frame.copy()) as picture:
            picture.thumbnail((params['image_max_side'], params['image_max_side']), Image.Resampling.LANCZOS)
            with io.BytesIO() as stream:
                picture.save(stream, format='JPEG', quality=params['jpeg_quality'])
                data = 'data:image/jpeg;base64,' + base64.b64encode(stream.getvalue()).decode('ascii')
        size += len(data)
        if size > MAX_REQUEST_BYTES:
            raise ValueError('llm_failure 图像超过32MiB；请减小图像尺寸或JPEG质量')
        encoded.append((name, data))
    return Snapshot(step, tuple(encoded))
