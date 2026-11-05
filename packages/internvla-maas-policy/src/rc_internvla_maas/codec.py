"""Verified MaaS __ndarray__ wire format with bounded numeric decoding."""
import math

import msgpack
import numpy as np

MAX_MESSAGE_BYTES = 4 * 1024 * 1024  # Incoming actions/metadata, not outgoing RGB images.


def _dtype(descriptor):
    if not isinstance(descriptor, str) or len(descriptor) > 16:
        raise ValueError('invalid ndarray dtype')
    try:
        dtype = np.dtype(descriptor)
    except (TypeError, ValueError):
        raise ValueError('invalid ndarray dtype') from None
    if dtype.hasobject or dtype.fields or dtype.subdtype or dtype.kind not in 'buif':
        raise ValueError('only plain numeric and boolean NumPy values are supported')
    return dtype


def _encode(value):
    if isinstance(value, (np.ndarray, np.generic)):
        _dtype(value.dtype.str)
        if isinstance(value, np.ndarray):
            return {b'__ndarray__': True, b'data': value.tobytes(),
                    b'dtype': value.dtype.str, b'shape': value.shape}
        data = value.item()
        if type(data) not in (bool, int, float):
            raise ValueError('NumPy scalar has no supported MessagePack value')
        return {b'__npgeneric__': True, b'data': data, b'dtype': value.dtype.str}
    raise TypeError(f'cannot encode {type(value).__name__}')


def pack(message):
    return msgpack.packb(message, default=_encode)


def _scalar(data, dtype):
    if dtype.kind == 'b':
        valid = type(data) is bool
    elif dtype.kind in 'ui':
        limits = np.iinfo(dtype)
        valid = type(data) is int and limits.min <= data <= limits.max
    else:
        valid = type(data) in (int, float)
    if not valid:
        raise ValueError('invalid NumPy scalar value')
    with np.errstate(over='raise', invalid='raise'):
        return dtype.type(data)


def _array(value):
    if any(key in value for key in (b'nd', 'nd', b'complex', 'complex',
                                    '__ndarray__', '__npgeneric__')):
        raise ValueError('unsupported legacy or text-key NumPy encoding')
    is_array, is_scalar = b'__ndarray__' in value, b'__npgeneric__' in value
    if not is_array and not is_scalar:
        return value
    marker = b'__ndarray__' if is_array else b'__npgeneric__'
    if (is_array and is_scalar) or value[marker] is not True:
        raise ValueError('invalid NumPy envelope marker')
    dtype = _dtype(value.get(b'dtype'))
    data = value.get(b'data')
    if is_scalar:
        return _scalar(data, dtype)
    shape = value.get(b'shape')
    if (not isinstance(shape, (list, tuple)) or len(shape) > 3
            or any(type(size) is not int or size <= 0 for size in shape)
            or not isinstance(data, bytes)):
        raise ValueError('invalid ndarray shape or buffer')
    size = math.prod(shape) * dtype.itemsize
    if size > MAX_MESSAGE_BYTES or size != len(data):
        raise ValueError('ndarray shape does not match its bounded buffer')
    return np.frombuffer(data, dtype=dtype).reshape(shape).copy()


def unpack(frame):
    if not isinstance(frame, bytes) or len(frame) > MAX_MESSAGE_BYTES:
        raise ValueError('MaaS requires a bounded binary MessagePack message')
    try:
        message = msgpack.unpackb(frame, raw=False, object_hook=_array,
                                  max_bin_len=MAX_MESSAGE_BYTES, max_str_len=MAX_MESSAGE_BYTES,
                                  max_array_len=4096, max_map_len=4096, max_ext_len=0)
    except (ValueError, TypeError, KeyError, OverflowError, FloatingPointError, msgpack.UnpackException):
        raise ValueError('invalid MaaS MessagePack message') from None
    if not isinstance(message, dict) or not isinstance(message.get('type'), str):
        raise ValueError('MaaS message requires a string type')
    return message
