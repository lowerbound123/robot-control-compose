import numpy as np
import pytest

from rc_policy_sdk.images import ImagePool, map_images


def test_guardian_retains_remote_frame_after_policy_death(tmp_path):
    from robot_control.external import ExternalResources
    pool=ImagePool(tmp_path/'frames', cameras=('head',), slots=2)
    worker=object()
    guard=ExternalResources(pool, tmp_path/'recovery.json', start=False)
    pool.publish('head', np.ones((2,2,3), np.uint8), 1.)
    original=pool.acquire()
    guard.handle(worker, dict(method='register',remote_id='model',endpoint='http://127.0.0.1:1',owner_id='owner',model_id='mock'))
    guard.handle(worker, dict(method='retain',remote_id='model',request_id='infer',images=original.descriptors))
    original.release()
    for i in range(5):
        pool.publish('head', np.full((2,2,3), i+5, np.uint8), i+2.)
    with map_images(original.descriptors) as images:
        assert images['head'].tobytes()==bytes([1])*12
    with pytest.raises(RuntimeError):
        guard.close_worker(worker, timeout=.1)
    assert (tmp_path/'recovery.json').exists()
    pool.close()
    assert pool._bytes > 0, 'unconfirmed reader must keep shared segments'
    guard.handle(worker, dict(method='released',remote_id='model',request_id='infer'))
    assert pool._bytes == 0


def test_guardian_rejects_foreign_worker_and_unknown_descriptors(tmp_path):
    from robot_control.external import ExternalResources
    pool=ImagePool(tmp_path/'frames', cameras=('head',))
    guard=ExternalResources(pool,tmp_path/'recovery',start=False)
    owner=object()
    guard.handle(owner,dict(method='register',remote_id='r',endpoint='http://127.0.0.1:1',owner_id='o',model_id='mock'))
    with pytest.raises(ValueError):
        guard.handle(object(),dict(method='retain',remote_id='r',request_id='q',images={}))
    pool.close()


def registered_reader(tmp_path):
    from robot_control.external import ExternalResources
    pool=ImagePool(tmp_path/'frames', cameras=('head',))
    guard=ExternalResources(pool,tmp_path/'recovery.json',start=False)
    worker=object()
    pool.publish('head',np.ones((2,2,3),np.uint8),1.)
    source=pool.acquire()
    guard.handle(worker,dict(method='register',remote_id='r',endpoint='http://localhost:8000',
                             owner_id='owner',model_id='mock',instance_id='old'))
    guard.handle(worker,dict(method='retain',remote_id='r',request_id='q',images=source.descriptors))
    source.release()
    return pool,guard,worker


def test_restart_between_status_and_unload_keeps_reader_pinned(tmp_path):
    pool,guard,worker=registered_reader(tmp_path)
    lease=guard._records['r']['leases']['q']
    def http(record,path,body=None,**kwargs):
        if path=='/v1/status': return {'instance_id':'old'}
        if path=='/v1/models/unload': return {'operation_id':'unload'}
        return {'status':'succeeded','result':{'instance_id':'new','state':'unloaded','worker_pid':None}}
    guard._http=http
    try:
        with pytest.raises(RuntimeError,match='restarted'):
            guard.close_worker(worker)
        assert not lease._released
        assert guard.recovery_path.exists()
    finally:
        if 'r' in guard._records:
            guard.handle(worker,dict(method='released',remote_id='r',request_id='q'))
        pool.close()


def test_guardian_unload_carries_expected_instance_and_reports_forced_cleanup(tmp_path):
    pool,guard,worker=registered_reader(tmp_path)
    bodies=[]
    def http(record,path,body=None,**kwargs):
        if path=='/v1/status': return {'instance_id':'old'}
        if path=='/v1/models/unload':
            bodies.append(body)
            return {'operation_id':'unload'}
        return {'status':'succeeded','result':{'instance_id':'old','state':'unloaded',
                                              'worker_pid':None,'cleanup_forced':True}}
    guard._http=http
    try:
        guard.begin_close(worker)
        assert guard.close_worker(worker) is True
        assert bodies and all(b['expected_instance_id']=='old' for b in bodies)
        assert not guard._records and not guard.recovery_path.exists()
    finally:
        for record in guard._records.values():
            for lease in record['leases'].values(): lease.release()
        pool.close()
