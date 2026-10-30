import sys
import time

import numpy as np
import posix_ipc
import pytest

from rc_policy_sdk.images import ImagePool, map_images
from host_helpers import LocalPolicyProcess as PolicyProcess


def test_retained_descriptor_pins_exact_frame(tmp_path):
    pool = ImagePool(tmp_path / 'images', cameras=('head',), slots=2)
    try:
        pool.publish('head', np.ones((2, 2, 3), np.uint8), 1.)
        lease = pool.acquire()
        retained = pool.retain(lease.descriptors)
        descriptors = retained.descriptors
        lease.release()
        for i in range(5):
            pool.publish('head', np.full((2, 2, 3), i + 3, np.uint8), i + 2.)
        with map_images(descriptors) as images:
            assert images['head'].tobytes() == bytes([1]) * 12
        retained.release()
    finally:
        pool.close()
    # Reclaiming the pinned segment must invalidate stale descriptors instead of
    # leaking the shared-memory object.
    with pytest.raises(posix_ipc.Error):
        with map_images(descriptors):
            pass


def test_optional_runtime_and_observe_during_blocked_policy(tmp_path):
    directory = tmp_path / 'policy'
    directory.mkdir()
    proof = tmp_path / 'seen'
    (directory / 'main.py').write_text('''
import time, numpy as np
from pathlib import Path
def initialize(config, *, runtime):
    return config['params']['proof']
def policy(context,state,action_chunk,images,config,*,runtime):
    time.sleep(.3)
    assert Path(context).read_text() == 'obs-2'
    row=np.zeros(14); row[0]=runtime.consumed_actions
    assert images.descriptors['head']['timestamp']==1.
    return row[None]
def observe(context,state,images,config,*,runtime):
    Path(context).write_text(runtime.observation_id)
def reset_context(context): pass
def release(context): pass
''')
    (directory / 'main.py').with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
    config = {'schema_version': 3, 'id': 'runtime',
              'params': {'proof': str(proof)}, 'hot_reloadable': []}
    worker = PolicyProcess(directory, config, python_command=[sys.executable],
                           init_timeout_s=2, inference_timeout_s=2, release_timeout_s=1)
    pool = ImagePool(tmp_path / 'frames', cameras=('head',))
    pool.publish('head', np.zeros((2, 2, 3), np.uint8), 1.)
    lease = pool.acquire()
    try:
        worker.start()
        call = dict(request_id=1, generation=2, config_version=1, state={'qpos': [0]*14},
                    images=lease.descriptors, action_chunk=[],
                    runtime={'observation_id':'obs-1','consumed_actions':7,'control_epoch':2})
        worker.submit(call)
        worker.observe({**call, 'request_id': 2, 'runtime': {**call['runtime'], 'observation_id':'obs-2'}})
        deadline = time.monotonic()+3
        result = None
        while time.monotonic()<deadline and result is None:
            result = worker.poll()
            time.sleep(.01)
        assert result['type']=='result', result
        assert result['actions'][0][0]==7
    finally:
        worker.close()
        lease.release()
        pool.close()


def test_release_exception_is_reported_as_unclean_exit(tmp_path):
    directory=tmp_path/'policy'; directory.mkdir()
    (directory/'main.py').write_text('''
def initialize(config): return None
def policy(*args): return [[0]*14]
def reset_context(context): pass
def release(context): raise RuntimeError('release cleanup failed')
''')
    (directory/'main.py').with_name("compose.yaml").write_text("services:\n  policy:\n    image: fixture:local\n")
    worker=PolicyProcess(directory,{'params':{},'hot_reloadable':[]},
                         python_command=[sys.executable],init_timeout_s=2,release_timeout_s=1)
    worker.start()
    assert worker.close() is True
    assert not worker.alive
