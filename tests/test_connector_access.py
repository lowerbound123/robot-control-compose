import threading

import pytest

from robot_control.connector_access import ConnectorAccess
from connector_helpers import MockRobot


def test_late_reader_never_returns_retired_connection_data():
    old, new = MockRobot(), MockRobot()
    old.connect(); new.connect()
    access = ConnectorAccess(old)
    entered, go = threading.Event(), threading.Event()
    read = old.read_state
    def delayed():
        entered.set()
        assert go.wait(2)
        return read()
    old.read_state = delayed
    results = []
    def reader():
        try:
            results.append(access.read_state())
        except RuntimeError as exc:
            results.append(str(exc))
    thread = threading.Thread(target=reader)
    thread.start()
    assert entered.wait(1)
    access.suspend(); access.bind(new)
    go.set(); thread.join(2)
    assert results == ['stale connector generation']
    assert access.read_state()['connector_generation'] == access.generation
    old.close(); access.close()


def test_image_boundary_never_revokes_live_lease_or_reaccepts_retired_descriptor():
    import numpy as np
    from rc_policy_sdk.images import ImagePool, map_images
    pool = ImagePool(cameras=('head',))
    try:
        pool.publish('head', np.zeros((2, 2, 3), np.uint8), 1.)
        lease = pool.acquire()
        old = lease.descriptors
        with pytest.raises(RuntimeError, match='readers'):
            pool.clear_latest()
        with map_images(old) as frames:
            assert frames['head'].shape == (2, 2, 3)
        lease.release()
        pool.clear_latest()
        with pytest.raises(ValueError, match='no longer'):
            with pool.retain(old):
                pass
        with pytest.raises((ValueError, RuntimeError)):
            with map_images(old):
                pass
        pool.publish('head', np.ones((2, 2, 3), np.uint8), 2.)
        with pool.acquire() as fresh:
            with map_images(fresh.descriptors) as frames:
                assert frames['head'][0, 0, 0] == 1
    finally:
        pool.close()
