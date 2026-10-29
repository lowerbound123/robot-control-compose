"""Versioned JSON snapshots. Workers map read-only; writes go to the RC broker."""
from collections.abc import MutableMapping
from contextlib import contextmanager
import fcntl
import json
import math
import mmap
import os
from pathlib import Path
import re
import struct
import threading
import time
import uuid
import zlib

import posix_ipc

PROTOCOL_VERSION = 1
MAX_BYTES = 3 * 1024 * 1024
_IDENTITY = struct.Struct('!8s24sI')
_SLOT = struct.Struct('!QII')
_MAGIC = b'RCGLOBAL'


class GlobalStateConflict(RuntimeError):
    pass


def validate_key(key):
    if type(key) is not str or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
        raise ValueError('global key must match [A-Za-z_][A-Za-z0-9_]*')
    return key


def _json_value(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise TypeError('global values must contain only finite built-in JSON types')


def validate_values(values):
    if type(values) is not dict:
        raise TypeError('global values must be a dict')
    for key, value in values.items():
        validate_key(key)
        _json_value(value)
    return values


def encode(values, capacity=MAX_BYTES):
    validate_values(values)
    raw = json.dumps(values, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > capacity:
        raise ValueError('global state capacity exceeded')
    return raw


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate JSON key')
        value[key] = item
    return value


def decode(raw):
    def invalid(_):
        raise ValueError('nonfinite JSON')
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=invalid)


class SharedState:
    def __init__(self, descriptor, segment, mapping, lock_fd, *, owner=False):
        self.descriptor, self.segment, self.mapping = descriptor, segment, mapping
        self.lock_fd, self.owner = lock_fd, owner
        self.local_lock = threading.Lock()
        self.closed = False

    @classmethod
    def create(cls, directory, values, *, max_bytes=1048576, timeout_s=2.0, store_id=None):
        raw = encode(values, max_bytes)
        identity = store_id or uuid.uuid4().hex[:24]
        if not re.fullmatch('[0-9a-f]{24}', identity):
            raise ValueError('invalid global store identity')
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ('global-' + identity + '.lock')
        descriptor = dict(protocol_version=PROTOCOL_VERSION, store_id=identity, shm_name='/rcg' + identity,
                          lock_path=str(path), max_bytes=max_bytes, timeout_s=timeout_s)
        lock_fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        segment = mapping = None
        try:
            size = _IDENTITY.size + 2 * (_SLOT.size + max_bytes)
            segment = posix_ipc.SharedMemory(descriptor['shm_name'], flags=posix_ipc.O_CREX, size=size, mode=0o600)
            if hasattr(os, 'posix_fallocate'):
                os.posix_fallocate(segment.fd, 0, size)
            mapping = mmap.mmap(segment.fd, size, access=mmap.ACCESS_WRITE)
            mapping[:_IDENTITY.size] = _IDENTITY.pack(_MAGIC, identity.encode(), max_bytes)
            result = cls(descriptor, segment, mapping, lock_fd, owner=True)
            with result.locked(write=True):
                result.publish(1, raw)
            return result
        except BaseException:
            if mapping is not None:
                mapping.close()
            if segment is not None:
                segment.unlink()
            os.close(lock_fd)
            path.unlink(missing_ok=True)
            raise
        finally:
            if segment is not None:
                segment.close_fd()

    @classmethod
    def attach(cls, descriptor):
        descriptor = dict(descriptor)
        capacity, identity = descriptor.get('max_bytes'), descriptor.get('store_id')
        if (descriptor.get('protocol_version') != PROTOCOL_VERSION or type(capacity) is not int
                or not 2 <= capacity <= MAX_BYTES or not isinstance(identity, str)
                or not re.fullmatch('[0-9a-f]{24}', identity)
                or descriptor.get('shm_name') != '/rcg' + identity):
            raise ValueError('incompatible global state descriptor')
        timeout = descriptor.get('timeout_s')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('invalid global state timeout')
        segment = posix_ipc.SharedMemory(descriptor['shm_name'], read_only=True)
        mapping = None
        lock_fd = None
        try:
            size = _IDENTITY.size + 2 * (_SLOT.size + capacity)
            page = os.sysconf('SC_PAGE_SIZE')
            if not size <= segment.size <= ((size + page - 1) // page) * page:
                raise ValueError('global state segment size mismatch')
            mapping = mmap.mmap(segment.fd, size, access=mmap.ACCESS_READ)
            if _IDENTITY.unpack(mapping[:_IDENTITY.size]) != (_MAGIC, identity.encode(), capacity):
                raise ValueError('global state identity mismatch')
            lock_fd = os.open(descriptor['lock_path'], os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            return cls(descriptor, segment, mapping, lock_fd)
        except BaseException:
            if mapping is not None:
                mapping.close()
            if lock_fd is not None:
                os.close(lock_fd)
            raise
        finally:
            segment.close_fd()

    @contextmanager
    def locked(self, *, write=False, deadline=None):
        deadline = min(deadline or float('inf'), time.monotonic() + self.descriptor['timeout_s'])
        if not self.local_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise TimeoutError('global state lock timeout')
        acquired = False
        try:
            if self.closed:
                raise RuntimeError('global state closed')
            while True:
                try:
                    fcntl.flock(self.lock_fd, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('global state lock timeout') from None
                    time.sleep(min(.005, max(0, deadline - time.monotonic())))
            yield
        finally:
            if acquired:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
            self.local_lock.release()

    def publish(self, version, raw):
        """Owner only, while holding the write lock. Header is the commit marker."""
        if not self.owner or not 0 < version < 2**64:
            raise ValueError('invalid global snapshot writer/version')
        capacity = self.descriptor['max_bytes']
        if len(raw) > capacity:
            raise ValueError('global state capacity exceeded')
        offset = _IDENTITY.size + (version % 2) * (_SLOT.size + capacity)
        self.mapping[offset:offset + _SLOT.size] = bytes(_SLOT.size)
        self.mapping[offset + _SLOT.size:offset + _SLOT.size + len(raw)] = raw
        self.mapping[offset:offset + _SLOT.size] = _SLOT.pack(version, len(raw), zlib.crc32(raw))

    def snapshot(self):
        capacity = self.descriptor['max_bytes']
        with self.locked():
            slots = []
            for index in range(2):
                offset = _IDENTITY.size + index * (_SLOT.size + capacity)
                version, length, checksum = _SLOT.unpack(self.mapping[offset:offset + _SLOT.size])
                if version:
                    if not 0 < length <= capacity:
                        raise ValueError('corrupt global snapshot length')
                    raw = self.mapping[offset + _SLOT.size:offset + _SLOT.size + length]
                    slots.append((version, checksum, raw))
        if not slots:
            raise ValueError('global snapshot unavailable')
        version, checksum, raw = max(slots, key=lambda slot: slot[0])
        if zlib.crc32(raw) != checksum:
            raise ValueError('corrupt global snapshot checksum')
        return version, validate_values(decode(raw))

    def close(self, *, unlink=False):
        with self.local_lock:
            if not self.closed:
                self.closed = True
                self.mapping.close()
                os.close(self.lock_fd)
            if unlink and self.owner:
                try:
                    self.segment.unlink()
                except posix_ipc.ExistentialError:
                    pass
                Path(self.descriptor['lock_path']).unlink(missing_ok=True)


class GlobalStateProxy(MutableMapping):
    def __init__(self, snapshot, patch, check=lambda: None):
        self._snapshot, self._patch, self._check = snapshot, patch, check

    def snapshot(self):
        self._check()
        return self._snapshot()

    def __getitem__(self, key):
        validate_key(key)
        return self.snapshot()[1][key]

    def __iter__(self):
        return iter(self.snapshot()[1])

    def __len__(self):
        return len(self.snapshot()[1])

    def __setitem__(self, key, value):
        self.patch({key: value})

    def __delitem__(self, key):
        self.patch({}, delete=(key,))

    def update(self, values=(), **kwargs):
        self.patch(dict(values, **kwargs))

    def patch(self, values, delete=(), expected_version=None):
        self._check()
        raw = encode(values)
        if isinstance(delete, str):
            raise TypeError('delete must be a sequence of keys')
        deleted = list(delete)
        for key in deleted:
            validate_key(key)
        if len(set(deleted)) != len(deleted) or set(deleted) & values.keys():
            raise ValueError('duplicate or conflicting global patch keys')
        if expected_version is not None and (type(expected_version) is not int or expected_version < 1):
            raise ValueError('expected_version must be a positive integer')
        return self._patch(decode(raw), deleted, expected_version)
