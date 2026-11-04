"""msgpack-numpy wire format, with validation before its optional pickle decoder."""
import math

import msgpack
import msgpack_numpy
import numpy as np

MAX_MESSAGE_BYTES = 1024 * 1024  # Incoming actions/metadata, not outgoing RGB images.


def pack(message):
    return msgpack_numpy.packb(message)


def _array(value):
    if b'complex' in value:
        raise ValueError('complex values are unsupported')
    if b'nd' not in value:
        return value
    if type(value[b'nd']) is not bool or value.get(b'kind', b'') != b'':
        raise ValueError('unsupported ndarray metadata')
    descriptor = value.get(b'type')
    if not isinstance(descriptor, str) or len(descriptor) > 16:
        raise ValueError('invalid ndarray dtype')
    try:
        dtype = np.dtype(descriptor)
    except (TypeError, ValueError):
        raise ValueError('invalid ndarray dtype') from None
    if dtype.hasobject or dtype.fields or dtype.subdtype or dtype.kind not in 'uif':
        raise ValueError('only plain numeric ndarrays are supported')
    data = value.get(b'data')
    shape = value.get(b'shape') if value[b'nd'] else ()
    if (not isinstance(shape, (list, tuple)) or len(shape) > 3
            or any(type(size) is not int or size <= 0 for size in shape)
            or not isinstance(data, bytes)):
        raise ValueError('invalid ndarray shape or buffer')
    size = math.prod(shape) * dtype.itemsize
    if size > MAX_MESSAGE_BYTES or size != len(data):
        raise ValueError('ndarray shape does not match its bounded buffer')
    return msgpack_numpy.decode(value)


def unpack(frame):
    if not isinstance(frame, bytes) or len(frame) > MAX_MESSAGE_BYTES:
        raise ValueError('MaaS requires a bounded binary MessagePack message')
    try:
        message = msgpack.unpackb(frame, raw=False, object_hook=_array,
                                  max_bin_len=MAX_MESSAGE_BYTES, max_str_len=MAX_MESSAGE_BYTES,
                                  max_array_len=4096, max_map_len=4096, max_ext_len=0)
    except (ValueError, TypeError, KeyError, OverflowError, msgpack.UnpackException):
        raise ValueError('invalid MaaS MessagePack message') from None
    if not isinstance(message, dict) or not isinstance(message.get('type'), str):
        raise ValueError('MaaS message requires a string type')
    return message
