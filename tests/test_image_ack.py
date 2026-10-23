import numpy as np
from types import SimpleNamespace
from rc_policy_sdk.images import ImagePool
from robot_control.controller import RobotController


def test_observation_error_without_release_proof_keeps_lease():
    pool=ImagePool(cameras=('head',),slots=1)
    pool.publish('head',np.ones((2,2),np.uint8),1.)
    lease=pool.acquire()
    worker=type('Reader', (), {'poll_observation': lambda self: {'type':'observation_error','error':'escaped view'}})()
    import threading
    controller=object.__new__(RobotController)
    controller._observe_inflight=(worker,lease,1)
    controller._owned_lock=threading.Lock()
    controller._confirmed_closed=set()
    controller._state='paused'
    controller._operation=None
    controller._worker=None
    try:
        controller._observe_tick()
        assert controller._observe_inflight is not None
        assert not pool.publish('head',np.zeros((2,2),np.uint8),2.)
    finally:
        lease.release();pool.close()


def test_observe_rejected_before_enqueue_releases_snapshot():
    import pytest
    pool = ImagePool(cameras=('head',), slots=1)
    pool.publish('head', np.ones((2, 2), np.uint8), 1.)
    lease = pool.acquire()
    controller = object.__new__(RobotController)
    controller._observe_inflight = None
    controller._state = 'running'
    controller._operation = None
    controller._identity = 'hold'
    controller._observation_serial = 1
    controller.buffer = SimpleNamespace(generation=1)
    controller.registry = SimpleNamespace(get=lambda _: SimpleNamespace(version=1))
    controller._snapshot = lambda: ({}, lease, {})
    def rejected(_): raise RuntimeError('host request queue is full')
    controller._worker = SimpleNamespace(supports_observe=True, observe=rejected)
    try:
        with pytest.raises(RuntimeError, match='queue is full'):
            controller._observe_tick()
        assert controller._observe_inflight is None
        assert pool.publish('head', np.zeros((2, 2), np.uint8), 2.)
        pool.close()
        assert pool._bytes == 0
    finally:
        lease.release(); pool.close()
