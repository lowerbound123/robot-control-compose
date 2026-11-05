"""Fixed-size RGB observations preserve aspect ratio, content and input frames."""
import importlib

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
