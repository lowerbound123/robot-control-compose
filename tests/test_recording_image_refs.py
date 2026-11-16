"""Recording retains owned pixels, never a borrowed shared-memory reader."""
import numpy as np


def descriptor(number):
    return {'pool_id': 'pool', 'frame_id': number, 'timestamp': float(number),
            'nbytes': 18, 'shape': [2, 3, 3], 'dtype': '|u1', 'shm_name': '/unused'}


def test_last_successful_publication_keeps_original_pixels_without_copy():
    from robot_control.recording_images import PublishedFrames
    index = PublishedFrames()
    pixels = np.full((2, 3, 3), 7, np.uint8)
    pixels.flags.writeable = False
    first = index.select({'head': (pixels, 1.)}, {'head': descriptor(1)}, epoch=0)
    assert first['head'][0] is pixels
    # No new publication: the camera cache can have changed, but the policy
    # still holds the prior pool frame. Recording must use exactly that frame.
    repeated = index.select({}, {'head': descriptor(1)}, epoch=0)
    assert repeated['head'][0] is pixels
    assert not repeated['head'][0].flags.writeable
    assert index.select({}, {'head': descriptor(2)}, epoch=0) == {}


def test_unknown_or_mutable_frames_and_old_sensor_epochs_are_not_substituted():
    from robot_control.recording_images import PublishedFrames
    index = PublishedFrames()
    pixels = np.zeros((2, 3, 3), np.uint8)
    assert index.select({'head': (pixels, 1.)}, {'head': descriptor(1)}, epoch=0) == {}
    pixels.flags.writeable = False
    assert index.select({'head': (pixels, 1.)}, {'head': descriptor(1)}, epoch=0)
    assert index.select({}, {'head': descriptor(1)}, epoch=1) == {}
    index.select({'head': (pixels, 1.)}, {'head': descriptor(1)}, epoch=1)
    index.clear()
    assert index.select({}, {'head': descriptor(1)}, epoch=1) == {}
