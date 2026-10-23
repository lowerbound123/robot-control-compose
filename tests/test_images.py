from pathlib import Path

import numpy as np
import pytest

from rc_policy_sdk.images import ImagePool, map_images


def test_triple_slots_never_overwrite_pinned_images(tmp_path):
    pool = ImagePool(tmp_path / "frames", cameras=("head",))
    leases = []
    try:
        for value in range(3):
            assert pool.publish("head", np.full((4, 5, 3), value, dtype=np.uint8), float(value))
            leases.append(pool.acquire())
        assert not pool.publish("head", np.full((4, 5, 3), 99, dtype=np.uint8), 3.0)
        for value, lease in enumerate(leases):
            with map_images(lease.descriptors) as frames:
                assert np.all(frames["head"] == value)
                assert not frames["head"].flags.writeable
                with pytest.raises(ValueError):
                    frames["head"][0, 0, 0] = 88
        leases[0].release(); leases[0].release()
        assert pool.publish("head", np.full((2, 2, 3), 7, dtype=np.uint8), 4.0)
        with pool.acquire() as latest:
            with map_images(latest.descriptors) as frames:
                assert frames["head"].shape == (2, 2, 3)
    finally:
        for lease in leases:
            lease.release()
        pool.close()



def test_acquire_requires_every_camera_and_validates_descriptors(tmp_path):
    pool = ImagePool(tmp_path / "frames", cameras=("head", "left_wrist"))
    try:
        pool.publish("head", np.zeros((2, 2, 3), dtype=np.uint8), 1.0)
        with pytest.raises(RuntimeError, match="left_wrist"):
            pool.acquire()
        with pytest.raises(ValueError):
            pool.publish("head", np.zeros((2, 2), dtype=object), 1.0)
        with pytest.raises(ValueError):
            pool.publish("head", np.zeros((2, 2), dtype=np.uint8), float("nan"))
        pool.publish("left_wrist", np.zeros((2, 2, 3), dtype=np.uint8), 1.0)
        lease = pool.acquire()
        bad = {key: dict(value) for key, value in lease.descriptors.items()}
        bad["head"]["shape"] = [10000, 10000, 3]
        with pytest.raises(ValueError):
            with map_images(bad):
                pass
        lease.release()
    finally:
        pool.close()


def test_independent_leases_pin_one_frame_until_both_release(tmp_path):
    pool = ImagePool(tmp_path / "frames", cameras=("head",), slots=1)
    pool.publish("head", np.ones((2, 2, 3), dtype=np.uint8), 1)
    first, second = pool.acquire(), pool.acquire()
    try:
        first.release()
        assert not pool.publish("head", np.zeros((2, 2, 3), dtype=np.uint8), 2)
        second.release()
        assert pool.publish("head", np.zeros((2, 2, 3), dtype=np.uint8), 2)
    finally:
        first.release(); second.release(); pool.close()


def test_retained_view_prevents_unmap_and_stays_readable(tmp_path):
    pool = ImagePool(tmp_path / "frames", cameras=("head",))
    pool.publish("head", np.ones((2, 2, 3), dtype=np.uint8), 1)
    lease = pool.acquire()
    try:
        with pytest.raises(BufferError, match="retained"):
            with map_images(lease.descriptors) as frames:
                retained = frames["head"]
        assert retained.sum() == 12
        del retained
    finally:
        lease.release(); pool.close()
