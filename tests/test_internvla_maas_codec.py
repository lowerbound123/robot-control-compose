import importlib

import numpy as np
import pytest

from internvla_maas_helpers import PACKAGE

msgpack = pytest.importorskip('msgpack')
msgpack_numpy = pytest.importorskip('msgpack_numpy')


def codec():
    assert (PACKAGE / 'rc_internvla_maas/codec.py').is_file(), 'safe MaaS codec missing'
    return importlib.import_module('rc_internvla_maas.codec')


def test_wire_matches_reference_client_in_both_directions():
    value = {'type': 'obs', 'images': {'front': np.arange(18, dtype=np.uint8).reshape(2, 3, 3)},
             'state': {'joint_pos': np.arange(14, dtype=np.float32)}}
    received = msgpack_numpy.unpackb(codec().pack(value))
    np.testing.assert_array_equal(received['images']['front'], value['images']['front'])
    np.testing.assert_array_equal(received['state']['joint_pos'], value['state']['joint_pos'])
    actions = np.ones((50, 14), np.float32)
    decoded = codec().unpack(msgpack_numpy.packb({'type': 'actions', 'actions': {'joint_pos': actions}}))
    np.testing.assert_array_equal(decoded['actions']['joint_pos'], actions)


@pytest.mark.parametrize('change', [
    {b'type': 'O', b'kind': b'O'}, {b'type': [('x', '<f4')], b'kind': b'V'},
    {b'shape': [-1, 14]}, {b'shape': [True, 14]}, {b'shape': [10**12]},
    {b'shape': [50, 13]}, {b'type': '<c8'}, {b'data': 'not-bytes'},
    {b'nd': 'yes'}, {b'kind': b'O'},
])
def test_malformed_arrays_rejected_before_library_decoder(change, monkeypatch):
    implementation = codec()
    array = {b'nd': True, b'type': '<f4', b'kind': b'', b'shape': [50, 14], b'data': bytes(2800)}
    array.update(change)
    def forbidden(*args, **kwargs):
        pytest.fail('unsafe metadata reached msgpack-numpy decode')
    monkeypatch.setattr(msgpack_numpy, 'decode', forbidden)
    with pytest.raises(ValueError):
        implementation.unpack(msgpack.packb({'type': 'actions', 'actions': {'joint_pos': array}}))


@pytest.mark.parametrize('raw', [b'\xc1', msgpack.packb([]), msgpack.packb({'type': 7}),
                                 'text-frame', b'x' * (1024 * 1024 + 1)],
                         ids=['invalid', 'list', 'bad-type', 'text', 'oversize'])
def test_non_messages_and_oversize_frames_rejected(raw):
    with pytest.raises(ValueError):
        codec().unpack(raw)
