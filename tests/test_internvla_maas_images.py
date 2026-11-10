"""Fixed-size RGB observations preserve aspect ratio, content and input frames."""
import importlib
import io

import numpy as np
import pytest

from internvla_maas_helpers import PACKAGE

pytest.importorskip('PIL')


def resize(frame):
    assert (PACKAGE / 'rc_internvla_maas/images.py').is_file(), 'MaaS image resize missing'
    return importlib.import_module('rc_internvla_maas.images').resize_rgb(frame)


@pytest.mark.parametrize('height,width,top,left,inner_height,inner_width', [
    (480, 640, 28, 0, 168, 224),
    (640, 480, 0, 28, 224, 168),
    (333, 1000, 74, 0, 75, 224),
    (1, 4096, 111, 0, 1, 224),
    (4096, 1, 0, 111, 224, 1),
    (1, 1, 0, 0, 224, 224),
    (224, 224, 0, 0, 224, 224),
])
def test_centered_black_letterbox_rgb_and_readonly_input(height, width, top, left, inner_height, inner_width):
    frame = np.empty((height, width, 3), np.uint8)
    frame[:] = (13, 29, 233)
    frame.flags.writeable = False
    actual = resize(frame)
    expected = np.zeros((224, 224, 3), np.uint8)
    expected[top:top + inner_height, left:left + inner_width] = (13, 29, 233)
    np.testing.assert_array_equal(actual, expected)
    assert actual.dtype == np.uint8 and actual.flags.c_contiguous
    assert actual.nbytes == 150528
    assert not frame.flags.writeable
    assert np.all(frame == (13, 29, 233))


def test_noncontiguous_image_keeps_all_four_corners_without_crop_or_channel_swap():
    source = np.zeros((4, 8, 3), np.uint8)
    source[:2, :4] = (255, 0, 0)
    source[:2, 4:] = (0, 255, 0)
    source[2:, :4] = (0, 0, 255)
    source[2:, 4:] = (255, 255, 255)
    frame = source[:, ::-1]
    frame.flags.writeable = False
    before = source.copy()
    actual = resize(frame)
    # 8x4 scales to224x112, with56 black rows above and below.
    np.testing.assert_array_equal(actual[56, 0], (0, 255, 0))
    np.testing.assert_array_equal(actual[56, -1], (255, 0, 0))
    np.testing.assert_array_equal(actual[167, 0], (255, 255, 255))
    np.testing.assert_array_equal(actual[167, -1], (0, 0, 255))
    assert not np.any(actual[:56]) and not np.any(actual[168:])
    np.testing.assert_array_equal(source, before)


def test_already_224_square_keeps_every_pixel():
    frame = np.random.default_rng(1).integers(0, 256, (224, 224, 3), dtype=np.uint8)
    before = frame.copy()
    np.testing.assert_array_equal(resize(frame), before)
    np.testing.assert_array_equal(frame, before)


def test_jpeg_bytes_decode_as_rgb_and_leave_readonly_input_unchanged():
    from PIL import Image
    from rc_internvla_maas.images import encode_jpeg
    source = np.empty((224, 224, 3), np.uint8)
    source[:] = (210, 30, 60)
    frame = source[:, ::-1]
    frame.flags.writeable = False
    payload = encode_jpeg(frame, 85)
    assert isinstance(payload, bytes) and payload[:2] == b'\xff\xd8'
    with Image.open(io.BytesIO(payload)) as decoded:
        assert decoded.format == 'JPEG' and decoded.mode == 'RGB'
        assert decoded.size == (224, 224)
        np.testing.assert_allclose(np.asarray(decoded)[112, 112], (210, 30, 60), atol=3)
    assert np.all(source == (210, 30, 60)) and not frame.flags.writeable


def test_jpeg_quality_changes_real_bitstream_and_quantization():
    from PIL import Image
    from rc_internvla_maas.images import encode_jpeg
    frame = np.random.default_rng(17).integers(0, 256, (224, 224, 3), dtype=np.uint8)
    low, high = (encode_jpeg(frame, quality) for quality in (85, 95))
    assert len(low) < len(high) < frame.nbytes
    with Image.open(io.BytesIO(low)) as lower, Image.open(io.BytesIO(high)) as higher:
        assert sum(higher.quantization[0]) < sum(lower.quantization[0])
