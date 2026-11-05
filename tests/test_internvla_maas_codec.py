import importlib

import numpy as np
import pytest

from internvla_maas_helpers import PACKAGE, images

msgpack = pytest.importorskip('msgpack')
import internvla_maas_reference as reference


def codec():
    assert (PACKAGE / 'rc_internvla_maas/codec.py').is_file(), 'safe MaaS codec missing'
    return importlib.import_module('rc_internvla_maas.codec')


def test_observation_wire_matches_successful_script():
    value = {'type': 'obs', 'images': images(),
             'state': {'joint_pos': np.arange(14, dtype=np.float32)}}
    packed = codec().pack(value)
    assert packed == reference.packb(value)
    raw = msgpack.unpackb(packed, raw=False)
    state = raw['state']['joint_pos']
    assert set(state) == {b'__ndarray__', b'data', b'dtype', b'shape'}
    assert state[b'__ndarray__'] is True
    assert state[b'dtype'] == '<f4' and state[b'shape'] == [14]
    assert state[b'data'] == value['state']['joint_pos'].tobytes()
    received = reference.unpackb(packed)
    for name, frame in value['images'].items():
        np.testing.assert_array_equal(received['images'][name], frame)
        assert received['images'][name].dtype == np.uint8
    np.testing.assert_array_equal(received['state']['joint_pos'], value['state']['joint_pos'])
    assert received['state']['joint_pos'].dtype == np.float32


def test_actions_from_successful_script_decode_to_owned_array():
    actions = np.arange(700, dtype=np.float32).reshape(50, 14)
    frame = reference.packb({'type': 'actions', 'actions': {'joint_pos': actions}})
    actual = codec().unpack(frame)['actions']['joint_pos']
    np.testing.assert_array_equal(actual, actions)
    assert actual.dtype == np.float32 and actual.shape == (50, 14)
    assert actual.flags.owndata and actual.flags.writeable
    actual[0, 0] = -123
    assert codec().unpack(frame)['actions']['joint_pos'][0, 0] == 0


@pytest.mark.parametrize('array', [
    np.arange(36, dtype=np.uint8).reshape(2, 6, 3)[:, ::2, :],
    np.arange(14, dtype=np.float32)[::-1],
    np.asfortranarray(np.arange(28, dtype=np.float32).reshape(2, 14)),
    np.arange(14, dtype='>f4'), np.array(True), np.array([True, False]),
], ids=['strided-image', 'negative-stride', 'fortran', 'big-endian', 'zero-dim', 'bool'])
def test_array_layout_and_dtype_match_reference(array):
    message = {'type': 'fixture', 'array': array}
    packed = codec().pack(message)
    assert packed == reference.packb(message)
    np.testing.assert_array_equal(reference.unpackb(packed)['array'], array)
    actual = codec().unpack(reference.packb(message))['array']
    np.testing.assert_array_equal(actual, array)
    assert actual.dtype == array.dtype and actual.shape == array.shape
    assert actual.flags.owndata


@pytest.mark.parametrize('scalar', [np.bool_(True), np.int16(-3), np.uint64(2**64 - 1),
                                   np.float16(1.25), np.float32(1.25)])
def test_numpy_scalars_match_successful_script(scalar):
    message = {'type': 'ready', 'value': scalar}
    packed = codec().pack(message)
    assert packed == reference.packb(message)
    raw = msgpack.unpackb(packed, raw=False)['value']
    assert raw == {b'__npgeneric__': True, b'data': scalar.item(), b'dtype': scalar.dtype.str}
    for actual in (reference.unpackb(packed)['value'],
                   codec().unpack(reference.packb(message))['value']):
        assert type(actual) is type(scalar) and actual == scalar


@pytest.mark.parametrize('value', [np.array([object()], dtype=object), np.zeros(1, dtype=[('x', 'f4')]),
                                  np.ones(1, dtype=np.complex64), np.complex64(1), np.void(b'ab'),
                                  np.array(['text']), np.datetime64('2026-11-05')])
def test_outgoing_unsupported_dtypes_are_rejected(value):
    with pytest.raises((TypeError, ValueError)):
        codec().pack({'type': 'obs', 'value': value})


@pytest.mark.parametrize('change', [
    {b'dtype': 'O'}, {b'dtype': [('x', '<f4')]}, {b'dtype': '<c8'},
    {b'dtype': '|V4'}, {b'dtype': 'not-a-dtype'}, {b'dtype': b'<f4'},
    {b'shape': [-1, 14]}, {b'shape': [True, 14]}, {b'shape': [0, 14]},
    {b'shape': [10**12]}, {b'shape': [50, 13]}, {b'shape': [1, 1, 50, 14]},
    {b'shape': '50,14'}, {b'data': 'not-bytes'}, {b'data': bytes(2801)},
    {b'__ndarray__': 'yes'}, {b'__ndarray__': False}, {b'__ndarray__': 1},
    {b'__npgeneric__': True},
])
def test_malformed_arrays_rejected_before_construction(change, monkeypatch):
    array = {b'__ndarray__': True, b'dtype': '<f4', b'shape': [50, 14], b'data': bytes(2800)}
    array.update(change)
    frame = msgpack.packb({'type': 'actions', 'actions': {'joint_pos': array}})
    def forbidden(*args, **kwargs):
        pytest.fail('malformed metadata reached ndarray construction')
    monkeypatch.setattr(np, 'frombuffer', forbidden)
    with pytest.raises(ValueError):
        codec().unpack(frame)


@pytest.mark.parametrize('missing', [b'data', b'dtype', b'shape'])
def test_incomplete_array_envelopes_are_rejected(missing):
    array = {b'__ndarray__': True, b'dtype': '<f4', b'shape': [50, 14], b'data': bytes(2800)}
    del array[missing]
    with pytest.raises(ValueError):
        codec().unpack(msgpack.packb({'type': 'actions', 'value': array}))


@pytest.mark.parametrize('change', [
    {b'__npgeneric__': False}, {b'__npgeneric__': 1}, {b'dtype': 'O'},
    {b'dtype': '<c8'}, {b'data': '1.25'}, {b'data': []}, {b'data': True},
    {b'dtype': '|b1', b'data': 1}, {b'dtype': '|u1', b'data': 256},
    {b'dtype': '<i2', b'data': 1.25}, {b'data': 1e100},
])
def test_malformed_scalars_are_rejected(change):
    scalar = {b'__npgeneric__': True, b'dtype': '<f4', b'data': 1.25}
    scalar.update(change)
    with pytest.raises(ValueError):
        codec().unpack(msgpack.packb({'type': 'ready', 'value': scalar}))


@pytest.mark.parametrize('array', [
    {b'nd': True, b'type': '<f4', b'kind': b'', b'shape': [14], b'data': bytes(56)},
    {b'nd': False, b'type': '<f4', b'data': bytes(4)},
    {b'complex': True, b'data': '1j'},
    {'__ndarray__': True, 'dtype': '<f4', 'shape': [14], 'data': bytes(56)},
    {'__npgeneric__': True, 'dtype': '<f4', 'data': 1.25},
])
def test_legacy_and_text_key_array_encodings_are_rejected(array):
    with pytest.raises(ValueError):
        codec().unpack(msgpack.packb({'type': 'actions', 'value': array}))


def test_incoming_limit_matches_successful_script_and_output_images_are_not_capped():
    message = {'type': 'ready', 'data': bytes(2 * 1024 * 1024)}
    assert codec().unpack(reference.packb(message)) == message
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    obs = {'type': 'obs', 'images': {name: frame for name in ('front', 'wrist_left', 'wrist_right')}}
    packed = codec().pack(obs)
    assert len(packed) > 4 * 1024 * 1024
    assert reference.unpackb(packed)['images']['front'].shape == (720, 1280, 3)


@pytest.mark.parametrize('raw', [b'\xc1', msgpack.packb([]), msgpack.packb({'type': 7}),
                                 'text-frame', b'x' * (4 * 1024 * 1024 + 1)],
                         ids=['invalid', 'list', 'bad-type', 'text', 'oversize'])
def test_non_messages_and_oversize_frames_rejected(raw):
    with pytest.raises(ValueError):
        codec().unpack(raw)
