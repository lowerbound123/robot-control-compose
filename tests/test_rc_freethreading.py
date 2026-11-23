"""Check real RC imports in a fresh interpreter, independent of pytest plugins."""
import mmap
import os
from pathlib import Path
import subprocess
import sys
import sysconfig
import uuid

import pytest


@pytest.mark.skipif(not sysconfig.get_config_var('Py_GIL_DISABLED'), reason='requires free-threaded CPython')
def test_rc_imports_keep_gil_disabled():
    root = Path(__file__).resolve().parents[1]
    paths = [root / 'src', root / 'packages/policy-sdk/src', root / 'packages/connector-sdk/src']
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(map(str, paths)))
    env.pop('PYTHON_GIL', None)  # Exercise automatic extension compatibility checks.
    subprocess.run([sys.executable, '-c', '''
import sys
from robot_control import controller, mcp_server, tui, recording
from scipy.spatial.transform import Rotation
from PIL import Image
from rc_policy_sdk.images import ImagePool
from rc_policy_sdk.global_state import SharedState
assert not sys._is_gil_enabled(), "an RC dependency enabled the GIL"
'''], env=env, check=True)


def test_posix_segments_interoperate_with_connector_process():
    from rc_policy_sdk import posix_shm

    name = '/rc' + uuid.uuid4().hex[:22]
    peer_name = '/rc' + uuid.uuid4().hex[:22]
    owner = posix_shm.create(name, 4)
    try:
        with mmap.mmap(owner.fd, 4) as view:
            view[:] = b'rcok'
        # The connector owns its interpreter and uses its existing native extension.
        subprocess.run([sys.executable, '-X', 'gil=1', '-c', '''
import mmap, posix_ipc, sys
reader = posix_ipc.SharedMemory(sys.argv[1], read_only=True)
with mmap.mmap(reader.fd, 4, access=mmap.ACCESS_READ) as view:
    assert view[:] == b'rcok'
reader.close_fd()
writer = posix_ipc.SharedMemory(sys.argv[2], flags=posix_ipc.O_CREX, size=4)
with mmap.mmap(writer.fd, 4) as view:
    view[:] = b'peer'
writer.close_fd()
''', name, peer_name], check=True)
        reader = posix_shm.open_readonly(peer_name)
        try:
            with mmap.mmap(reader.fd, 4, access=mmap.ACCESS_READ) as view:
                assert view[:] == b'peer'
            with pytest.raises(OSError):
                mmap.mmap(reader.fd, 4, access=mmap.ACCESS_WRITE)
        finally:
            reader.close_fd()
    finally:
        owner.close_fd()
        owner.unlink()
        try:
            posix_shm.unlink(peer_name)
        except posix_shm.MissingSegment:
            pass
