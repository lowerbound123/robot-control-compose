"""Run in a standalone Linux container with --shm-size=1m (not the live IPC anchor)."""
import errno
import numpy as np
from rc_policy_sdk.images import ImagePool

pool = ImagePool(cameras=('head',))
try:
    try:
        pool.publish('head', np.zeros((2048, 1024), np.uint8), 1.)
    except OSError as exc:
        assert exc.errno == errno.ENOSPC, exc
    else:
        raise AssertionError('expected exhausted Docker tmpfs')
    assert pool._bytes == 0
finally:
    pool.close()
print('ENOSPC handled; no leaked shared-memory allocation')
