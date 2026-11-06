"""Leased POSIX shared memory; image payloads never pass through RPC."""

from contextlib import contextmanager
import math
import struct
import mmap
import os
import re
import uuid
import posix_ipc
import threading

import numpy as np

MAX_IMAGE_BYTES = 256 * 1024 * 1024
_HEADER = struct.Struct("!QQ")  # payload length and immutable frame identity


class ImageLease:
    def __init__(self, pool, selected):
        self.descriptors = {camera: dict(slot["descriptor"]) for camera, slot in selected.items()}
        self._pool = pool
        self._selected = selected
        self._released = False

    def release(self):
        with self._pool._lock:
            if self._released:
                return
            self._released = True
            for slot in self._selected.values():
                slot["leases"] -= 1
            self._pool._cleanup_if_closed()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()


class ImagePool:
    """Single writer, bounded POSIX segments; descriptors are published after writes.

    ``directory`` is accepted for source API compatibility; no files are created
    there. The operating system owns the shared-memory backing store.
    """
    def __init__(self, directory=None, cameras=("head", "left_wrist", "right_wrist"), slots=3,
                 max_bytes=512 * 1024 * 1024):
        if not cameras or len(set(cameras)) != len(cameras) or type(slots) is not int or slots < 1:
            raise ValueError("unique cameras and a positive slot count are required")
        if any(not isinstance(c, str) or not c or "/" in c or c in (".", "..") for c in cameras):
            raise ValueError("camera names must be simple names")
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self.pool_id = uuid.uuid4().hex[:12]
        self.max_bytes = max_bytes
        self._bytes = 0
        self._serial = 0
        self._lock = threading.RLock()
        self._closed = False
        self._latest = {}
        self._slots = {camera: [dict(leases=0, descriptor=None, segment=None, mapping=None, size=0)
                               for _ in range(slots)] for camera in cameras}

    def _dispose(self, slot):
        if slot['mapping'] is not None:
            slot['mapping'].close()
            slot['segment'].unlink()
            self._bytes -= slot['size'] + _HEADER.size
            slot.update(mapping=None, segment=None, size=0, descriptor=None)

    def _allocate(self, size):
        if self._bytes + size > self.max_bytes:
            raise MemoryError("shared image pool capacity exceeded")
        self._serial += 1
        name = f"/rc{self.pool_id}{self._serial:010x}"
        segment = posix_ipc.SharedMemory(name, flags=posix_ipc.O_CREX, size=size)
        try:
            # Reserve real tmpfs pages before touching mmap, avoiding SIGBUS on
            # Linux when another pool has already exhausted /dev/shm.
            if hasattr(os, 'posix_fallocate'):
                os.posix_fallocate(segment.fd, 0, size)
            mapping = mmap.mmap(segment.fd, size, access=mmap.ACCESS_WRITE)
        except BaseException:
            segment.unlink()
            raise
        finally:
            segment.close_fd()
        self._bytes += size
        return segment, mapping

    def publish(self, camera, image, timestamp):
        if camera not in self._slots:
            raise ValueError(f"unknown camera: {camera}")
        if (not isinstance(image, np.ndarray) or image.dtype.kind not in "buif"
                or image.ndim not in (2, 3) or any(size <= 0 for size in image.shape)
                or image.nbytes > MAX_IMAGE_BYTES):
            raise ValueError("image must be a nonempty numeric 2D/3D ndarray of bounded size")
        if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
            raise ValueError("image timestamp must be finite")
        with self._lock:
            if self._closed:
                raise RuntimeError("image pool is closed")
            latest = self._latest.get(camera)
            candidates = [slot for slot in self._slots[camera] if slot['leases'] == 0]
            if not candidates:
                return False
            slot = next((s for s in candidates if s is not latest), candidates[0])
            if slot['size'] != image.nbytes:
                segment, mapping = self._allocate(image.nbytes + _HEADER.size)
                self._dispose(slot)
                slot.update(segment=segment, mapping=mapping, size=image.nbytes)
            slot['mapping'][_HEADER.size:] = memoryview(np.ascontiguousarray(image)).cast('B')
            self._serial += 1
            slot['mapping'][:_HEADER.size] = _HEADER.pack(image.nbytes, self._serial)
            slot['descriptor'] = dict(shm_name=slot['segment'].name, pool_id=self.pool_id,
                                      frame_id=self._serial, dtype=image.dtype.str, shape=list(image.shape),
                                      timestamp=float(timestamp), nbytes=image.nbytes)
            self._latest[camera] = slot
            return True

    def acquire(self):
        with self._lock:
            if self._closed:
                raise RuntimeError("image pool is closed")
            missing = [c for c in self._slots if c not in self._latest]
            if missing:
                raise RuntimeError(f"required camera images missing: {', '.join(missing)}")
            return self.retain({c: s['descriptor'] for c, s in self._latest.items()})

    def retain(self, descriptors):
        with self._lock:
            if self._closed:
                raise RuntimeError("image pool is closed")
            selected = {}
            for camera, descriptor in descriptors.items():
                slot = next((s for s in self._slots.get(camera, ()) if s['descriptor'] == descriptor), None)
                if slot is None:
                    raise ValueError("retained image no longer belongs to this pool")
                selected[camera] = slot
            for slot in selected.values():
                slot['leases'] += 1
            return ImageLease(self, selected)

    def _cleanup_if_closed(self):
        if self._closed:
            for slots in self._slots.values():
                for slot in slots:
                    if not slot['leases']:
                        self._dispose(slot)

    def close(self):
        with self._lock:
            self._closed = True
            self._cleanup_if_closed()


@contextmanager
def map_images(descriptors):
    if not isinstance(descriptors, dict) or not descriptors or len(descriptors) > 32:
        raise ValueError("image descriptors must be a nonempty camera mapping")
    images, mappings = ImageBatch(descriptors), []
    try:
        for camera, descriptor in descriptors.items():
            if not isinstance(camera, str) or not isinstance(descriptor, dict):
                raise ValueError("invalid image descriptor")
            shape = descriptor.get("shape")
            if not isinstance(shape, list) or len(shape) not in (2, 3) or any(type(size) is not int or size <= 0 for size in shape):
                raise ValueError("invalid image shape")
            try:
                dtype = np.dtype(descriptor["dtype"])
            except (KeyError, TypeError) as exc:
                raise ValueError("invalid image dtype") from exc
            size = math.prod(shape) * dtype.itemsize
            if dtype.kind not in "buif" or size <= 0 or size > MAX_IMAGE_BYTES or descriptor.get("nbytes") != size:
                raise ValueError("invalid image byte length")
            timestamp = descriptor.get("timestamp")
            if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
                raise ValueError("invalid image timestamp")
            name = descriptor.get('shm_name')
            if (not isinstance(name, str) or not re.fullmatch(r'/rc[0-9a-f]{22}', name)
                    or descriptor.get('pool_id') != name[3:15]
                    or type(descriptor.get('frame_id')) is not int or descriptor['frame_id'] < 1):
                raise ValueError("invalid shared image identity")
            segment = posix_ipc.SharedMemory(name, read_only=True)
            try:
                if segment.size < size + _HEADER.size:
                    raise ValueError("shared image length does not match descriptor")
                mapping = mmap.mmap(segment.fd, size + _HEADER.size, access=mmap.ACCESS_READ)
            finally:
                segment.close_fd()
            mappings.append(mapping)
            if _HEADER.unpack(mapping[:_HEADER.size]) != (size, descriptor["frame_id"]):
                raise ValueError("stale frame or invalid shared image length")
            # frombuffer keeps an exported memoryview: a policy retaining the
            # image prevents mmap.close, instead of leaving an invalid pointer.
            images[camera] = np.frombuffer(mapping, dtype=dtype, count=math.prod(shape), offset=_HEADER.size).reshape(shape)
        yield images
    finally:
        images.clear()
        retained_view = False
        for mapping in mappings:
            try:
                mapping.close()
            except BufferError:
                retained_view = True
        if retained_view:
            raise BufferError("policy retained an image view beyond its call")


class ImageBatch(dict):
    """Borrowed read-only views plus descriptors for explicitly leased consumers."""
    def __init__(self, descriptors):
        super().__init__()
        self.descriptors = {name: dict(value) for name, value in descriptors.items()}
