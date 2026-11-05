"""Frozen codec from the user's verified diagnose_maas_ndarray.py.

Keep this independent of the production codec: it represents the deployed
gateway's wire format, not the client's additional input validation.
"""
import msgpack
import numpy as np

MAX_MESSAGE_BYTES = 4 * 1024 * 1024


def pack_array(obj):
    if isinstance(obj, np.ndarray):
        if obj.dtype.kind in 'V0cO':
            raise ValueError(f'unsupported dtype {obj.dtype}')
        return {b'__ndarray__': True, b'data': obj.tobytes(),
                b'dtype': obj.dtype.str, b'shape': obj.shape}
    if isinstance(obj, np.generic):
        return {b'__npgeneric__': True, b'data': obj.item(), b'dtype': obj.dtype.str}
    raise TypeError(f'cannot encode {type(obj).__name__}')


def unpack_array(obj):
    if b'__ndarray__' in obj:
        data, dtype, shape = obj[b'data'], obj[b'dtype'], obj[b'shape']
        if not isinstance(data, bytes) or not isinstance(shape, (list, tuple)):
            raise ValueError('malformed __ndarray__ envelope')
        return np.frombuffer(data, dtype=np.dtype(dtype)).reshape(shape).copy()
    if b'__npgeneric__' in obj:
        return np.dtype(obj[b'dtype']).type(obj[b'data'])
    return obj


def packb(obj):
    return msgpack.packb(obj, default=pack_array)


def unpackb(frame):
    return msgpack.unpackb(frame, object_hook=unpack_array,
                           max_bin_len=MAX_MESSAGE_BYTES)
