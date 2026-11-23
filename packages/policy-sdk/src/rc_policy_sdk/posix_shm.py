"""Named segments for RC images and globals, with explicit owner-only unlink."""
import os
import sysconfig


if sysconfig.get_config_var('Py_GIL_DISABLED'):
    # Use CPython's POSIX primitives directly. SharedMemory(track=False) also
    # uses these, but cannot open a reader's descriptor with O_RDONLY.
    from _posixshmem import shm_open, shm_unlink as unlink

    MissingSegment = FileNotFoundError

    class _Segment:
        def __init__(self, name, fd):
            self.name = name
            self.fd = fd

        @property
        def size(self):
            return os.fstat(self.fd).st_size

        def close_fd(self):
            os.close(self.fd)
            self.fd = -1

        def unlink(self):
            unlink(self.name)

    def create(name, size):
        segment = _Segment(name, shm_open(name, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600))
        try:
            os.ftruncate(segment.fd, size)
        except BaseException:
            try:
                segment.unlink()
            finally:
                segment.close_fd()
            raise
        return segment

    def open_readonly(name):
        return _Segment(name, shm_open(name, os.O_RDONLY, 0))

else:
    import posix_ipc
    from posix_ipc import ExistentialError as MissingSegment, unlink_shared_memory as unlink

    def create(name, size):
        return posix_ipc.SharedMemory(name, flags=posix_ipc.O_CREX, mode=0o600, size=size)

    def open_readonly(name):
        return posix_ipc.SharedMemory(name, read_only=True)
