"""Immutable, owned JPEG snapshots; no shared image views survive a callback."""
import base64
from collections.abc import Sequence
from dataclasses import dataclass
import io

import numpy as np
from PIL import Image

from .prompt_variables import FrameText, RequestText
from .request_size import MAX_REQUEST_BYTES

CAMERAS = ('head', 'left_wrist', 'right_wrist')


@dataclass(frozen=True)
class Snapshot:
    step: int
    images: tuple[tuple[str, str], ...]
    captions: FrameText | None = None


@dataclass(frozen=True)
class CheckInput(Sequence[Snapshot]):
    """One request owns its current instructions and a stable window of frames."""
    frames: tuple[Snapshot, ...]
    text: RequestText

    def __getitem__(self, index):
        return self.frames[index]

    def __len__(self):
        return len(self.frames)


def encode_snapshot(images, step, params, *, captions=None):
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
    return Snapshot(step, tuple(encoded), captions)
