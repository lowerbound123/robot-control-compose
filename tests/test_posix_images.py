import mmap
import numpy as np
import posix_ipc
import pytest

from rc_policy_sdk.images import ImagePool, map_images


def test_named_readonly_segments_remain_until_last_reader_releases():
    pool = ImagePool(cameras=('head',), slots=1)
    first = second = None
    try:
        assert pool.publish('head', np.full((2, 3, 3), 7, np.uint8), 1.)
        first, second = pool.acquire(), pool.acquire()
        descriptor = first.descriptors['head']
        assert 'shm_name' in descriptor and 'path' not in descriptor
        assert descriptor['pool_id'] and descriptor['frame_id']
        with map_images(first.descriptors) as images:
            assert images['head'].sum() == 126
            assert not images['head'].flags.writeable
        first.release()
        assert not pool.publish('head', np.zeros((2, 3, 3), np.uint8), 2.)
        pool.close()
        segment = posix_ipc.SharedMemory(descriptor['shm_name'], read_only=True)
        segment.close_fd()
        second.release()
        with pytest.raises(posix_ipc.ExistentialError):
            posix_ipc.SharedMemory(descriptor['shm_name'])
    finally:
        if first: first.release()
        if second: second.release()
        pool.close()


def test_resize_changes_segment_identity_and_budget_is_enforced():
    pool = ImagePool(cameras=('head',), slots=2, max_bytes=64)
    try:
        pool.publish('head', np.ones((2, 2), np.uint8), 1.)
        with pool.acquire() as lease:
            old = lease.descriptors['head']['shm_name']
            pool.publish('head', np.full((3, 3), 2, np.uint8), 2.)
            with pool.acquire() as other:
                assert old != other.descriptors['head']['shm_name']
            with map_images(lease.descriptors) as images:
                assert images['head'].sum() == 4
        with pytest.raises((ValueError, MemoryError)):
            pool.publish('head', np.zeros((9, 9), np.uint8), 3.)
    finally:
        pool.close()


def test_escaped_view_prevents_recycling_and_invalid_descriptor_cannot_resize():
    pool = ImagePool(cameras=('head',), slots=1)
    try:
        pool.publish('head', np.ones((2, 2), np.uint8), 1.)
        lease = pool.acquire()
        bad = {'head': {**lease.descriptors['head'], 'shape': [3, 3], 'nbytes': 9}}
        with pytest.raises(ValueError):
            with map_images(bad): pass
        with pytest.raises(BufferError):
            with map_images(lease.descriptors) as images:
                retained = images['head']
        assert not pool.publish('head', np.zeros((2, 2), np.uint8), 2.)
        assert retained.sum() == 4
        del retained
        lease.release()
    finally:
        pool.close()
