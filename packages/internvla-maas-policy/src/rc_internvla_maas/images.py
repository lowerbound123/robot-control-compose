"""MaaS wire images: aspect-preserving RGB resize with centered black padding."""
import io

import numpy as np
from PIL import Image

IMAGE_SIZE = 224


def resize_rgb(frame):
    """Letterbox validated RGB uint8 input without modifying the source frame."""
    height, width = frame.shape[:2]
    if (height, width) == (IMAGE_SIZE, IMAGE_SIZE):
        return frame
    scale = IMAGE_SIZE / max(height, width)
    # Pillow's generic contain/pad may round a very thin image down to zero.
    resized_width = max(1, round(width * scale))
    resized_height = max(1, round(height * scale))
    resized = Image.fromarray(frame).resize(
        (resized_width, resized_height), resample=Image.Resampling.BILINEAR)
    result = np.zeros((IMAGE_SIZE, IMAGE_SIZE, 3), dtype=np.uint8)
    top = (IMAGE_SIZE - resized_height) // 2
    left = (IMAGE_SIZE - resized_width) // 2
    result[top:top + resized_height, left:left + resized_width] = np.asarray(resized)
    return result


def encode_jpeg(frame, quality):
    """Encode an owned RGB snapshot; the caller supplies validated quality."""
    with io.BytesIO() as stream:
        Image.fromarray(frame).save(stream, format='JPEG', quality=quality)
        return stream.getvalue()
