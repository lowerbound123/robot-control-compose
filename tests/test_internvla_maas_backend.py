import importlib
import io
import threading
import time
import sys
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from internvla_maas_helpers import PACKAGE, configured, images, model_server, settings, wait

pytest.importorskip('websockets')
pytest.importorskip('msgpack')
pytestmark = pytest.mark.skipif(sys.version_info < (3, 11), reason='MaaS policy image uses Python 3.11')


def backend(config):
    assert (PACKAGE / 'rc_internvla_maas/backend.py').is_file(), 'MaaS backend missing'
    return importlib.import_module('rc_internvla_maas.backend').MaasBackend(config)


def test_real_handshake_raw_rgb_state_offsets_and_absolute_actions(tmp_path):
    with model_server() as server:
        raw = settings(server.address + '/gateway/')
        raw['service']['robot_id'] = 'robot /?'
        raw['observations'] = {'state_offsets': {'0': .25}}
        model = backend(configured(tmp_path, raw))
        try:
            model.load()
            model.warmup()
            assert not server.requests
            hello = server.hellos[0]
            assert hello['path'] == '/gateway/v1/robot/internvla_a15/act?robot_id=robot+%2F%3F'
            assert hello['message'] == dict(type='robot_hello', robot_id='robot /?', embodiment='test-dual-arm',
                                           dof=14, images=['front', 'wrist_left', 'wrist_right'],
                                           state=['joint_pos'], action_spaces=['joint_pos'], image_encodings=['raw'])
            state = np.arange(14, dtype=np.float32)
            actual = model.predict(state, images(), 'instruction')
            assert actual.shape == (50, 14) and actual.dtype == np.float32
            np.testing.assert_array_equal(actual[0], [.25, *range(1, 14)])
            np.testing.assert_array_equal(state, np.arange(14))
            request = server.requests[0]
            assert request['seq'] == 0 and request['instruction'] == 'instruction'
            assert request['t'] > 0 and request['episode_id']
            assert set(request['images']) == {'front', 'wrist_left', 'wrist_right'}
            assert request['images']['front'].shape == (224, 224, 3)
            assert request['images']['front'].dtype == np.uint8
            assert request['images']['wrist_right'][112, 112, 0] == 3
            model.predict(state, images(), 'next')
            assert server.requests[1]['seq'] == 1
            assert server.requests[1]['episode_id'] == request['episode_id']
        finally:
            model.close()
            model.close()
        wait(lambda: not server.connections)


def test_three_camera_frames_are_resized_before_ndarray_transmission(tmp_path):
    with model_server() as server:
        model = backend(configured(tmp_path, settings(server.address)))
        frames = {name: np.full((480, 640, 3), color, np.uint8)
                  for name, color in (('head', (255, 0, 0)), ('left_wrist', (0, 255, 0)), ('right_wrist', (0, 0, 255)))}
        originals = {name: frame.copy() for name, frame in frames.items()}
        try:
            model.load()
            model.predict(np.zeros(14, np.float32), frames, 'task')
            request = server.requests[0]
            for role, color in (('front', (255, 0, 0)), ('wrist_left', (0, 255, 0)), ('wrist_right', (0, 0, 255))):
                frame = request['images'][role]
                assert isinstance(frame, np.ndarray) and frame.shape == (224, 224, 3)
                assert frame.dtype == np.uint8
                assert not np.any(frame[:28]) and not np.any(frame[196:])
                assert np.all(frame[28:196] == color)
            for name in frames:
                np.testing.assert_array_equal(frames[name], originals[name])
            snapshot = model.monitor()
            assert snapshot['last_source_image_bytes'] == 2764800
            assert snapshot['last_image_bytes'] == 451584
            assert 451584 < snapshot['last_request_bytes'] < 453000
            assert snapshot['resize_samples'] == 1 and snapshot['last_resize_ms'] >= 0
        finally:
            model.close()


@pytest.mark.parametrize('quality', [85, 95, 100])
def test_jpeg_wire_envelopes_decode_three_rgb_cameras_and_report_payload_sizes(tmp_path, quality):
    from PIL import Image
    with model_server() as server:
        raw = settings(server.address)
        raw['observations'] = {'jpeg': True, 'jpeg_quality': quality}
        model = backend(configured(tmp_path, raw))
        colors = {'head': (210, 30, 60), 'left_wrist': (30, 210, 60), 'right_wrist': (30, 60, 210)}
        frames = {name: np.full((480, 640, 3), color, np.uint8) for name, color in colors.items()}
        for frame in frames.values():
            frame.flags.writeable = False
        try:
            model.load()
            assert server.hellos[0]['message']['image_encodings'] == ['raw', 'jpeg']
            state = np.arange(14, dtype=np.float32)
            actual = model.predict(state, frames, 'JPEG test')
            np.testing.assert_array_equal(actual[0], state)
            request = server.requests[0]
            size = 0
            for name, role in (('head', 'front'), ('left_wrist', 'wrist_left'), ('right_wrist', 'wrist_right')):
                envelope = request['images'][role]
                assert set(envelope) == {'enc', 'data'} and envelope['enc'] == 'jpeg'
                assert isinstance(envelope['data'], bytes)
                size += len(envelope['data'])
                with Image.open(io.BytesIO(envelope['data'])) as decoded:
                    assert decoded.format == 'JPEG' and decoded.mode == 'RGB' and decoded.size == (224, 224)
                    pixels = np.asarray(decoded)
                    np.testing.assert_allclose(pixels[112, 112], colors[name], atol=3)
                    assert pixels[:20].max() <= 3 and pixels[204:].max() <= 3
                assert np.all(frames[name] == colors[name]) and not frames[name].flags.writeable
            snapshot = model.monitor()
            assert snapshot['last_source_image_bytes'] == 2764800
            assert snapshot['last_image_bytes'] == size < 451584
            assert size < snapshot['last_request_bytes'] < size + 1500
            assert snapshot['encode_samples'] == 1 and snapshot['last_encode_ms'] >= 0
        finally:
            model.close()


def test_raw_mode_does_not_invoke_jpeg_encoder(tmp_path, monkeypatch):
    from rc_internvla_maas import backend as module
    def forbidden(*_):
        raise AssertionError('raw mode must not encode JPEG')
    monkeypatch.setattr(module, 'encode_jpeg', forbidden)
    with model_server() as server:
        model = backend(configured(tmp_path, settings(server.address)))
        try:
            model.load()
            model.predict(np.zeros(14, np.float32), images(), 'raw test')
            assert isinstance(server.requests[0]['images']['front'], np.ndarray)
            assert model.monitor()['encode_samples'] == 0
            assert model.monitor()['last_image_bytes'] == 451584
        finally:
            model.close()


def test_jpeg_encoding_failure_faults_without_sending_or_falling_back(tmp_path, monkeypatch):
    from rc_internvla_maas import backend as module
    def broken(*_):
        raise OSError('fixture-key should never be logged')
    monkeypatch.setattr(module, 'encode_jpeg', broken)
    with model_server() as server:
        raw = settings(server.address)
        raw['observations'] = {'jpeg': True}
        model = backend(configured(tmp_path, raw))
        try:
            model.load()
            with pytest.raises(RuntimeError) as error:
                model.predict(np.zeros(14, np.float32), images(), 'encode failure')
            assert 'fixture-key' not in str(error.value)
            with pytest.raises(RuntimeError):
                model.predict(np.zeros(14, np.float32), images(), 'no retry')
            assert not server.requests
        finally:
            model.close()


def test_model_quality_setting_changes_jpeg_received_by_peer(tmp_path):
    from PIL import Image
    frames = {name: np.random.default_rng(index).integers(0, 256, (224, 224, 3), dtype=np.uint8)
              for index, name in enumerate(('head', 'left_wrist', 'right_wrist'))}
    results = []
    with model_server() as server:
        for quality in (85, 95):
            raw = settings(server.address)
            raw['observations'] = {'jpeg': True, 'jpeg_quality': quality}
            model = backend(configured(tmp_path, raw))
            try:
                model.load()
                model.predict(np.zeros(14, np.float32), frames, 'quality test')
                data = server.requests[-1]['images']['front']['data']
                with Image.open(io.BytesIO(data)) as decoded:
                    results.append((len(data), sum(decoded.quantization[0])))
            finally:
                model.close()
        assert results[0][0] < results[1][0]
        assert results[0][1] > results[1][1]


@pytest.mark.parametrize('patch', [
    {'protocol': 'other'}, {'model': 'wrong'},
    {'requires': {'images': {'front': [224, 224]}, 'state': ['joint_pos']}},
    {'requires': {'images': {n: [224, 224] for n in ('front', 'wrist_left', 'wrist_right')}, 'state': ['qpos']}},
    {'provides': {'space': 'joint_pos', 'fields': ['joint_pos'], 'horizon': 49, 'control_hz': 30}},
    {'provides': {'space': 'joint_pos', 'fields': ['joint_pos'], 'horizon': 50, 'control_hz': 100}},
])
def test_incompatible_hello_fails_before_observations(tmp_path, patch):
    with model_server(hello_patch=patch) as server:
        model = backend(configured(tmp_path, settings(server.address)))
        try:
            with pytest.raises(RuntimeError):
                model.load()
            assert not server.requests
        finally:
            model.close()


@pytest.mark.parametrize('options,expected', [({'reject_status': 401}, '401'),
                                              ({'reject_status': 404}, '404'),
                                              ({'close_code': 4400}, '4400'),
                                              ({'close_code': 4409}, '4409')])
def test_transport_errors_are_actionable_and_redacted(tmp_path, options, expected, caplog):
    with model_server(**options) as server:
        model = backend(configured(tmp_path, settings(server.address)))
        try:
            with pytest.raises(RuntimeError, match=expected) as error:
                model.load()
            assert 'fixture-key' not in str(error.value)
            assert 'fixture-key' not in caplog.text
        finally:
            model.close()


@pytest.mark.parametrize('body', [
    {'type': 'stop', 'reason': 'fixture-key stopped'},
    {'type': 'error', 'code': 'FAILED', 'message': 'fixture-key failed'},
    {'type': 'actions', 'space': 'joint_pos', 'mode': 'delta', 'actions': {'joint_pos': np.zeros((50, 14), np.float32)}},
    {'type': 'actions', 'space': 'joint_pos', 'mode': 'absolute', 'actions': {'joint_pos': np.zeros((49, 14), np.float32)}},
    {'type': 'actions', 'space': 'joint_pos', 'mode': 'absolute', 'actions': {'joint_pos': np.full((50, 14), np.nan, np.float32)}},
    {'type': 'actions', 'space': 'joint_pos', 'mode': 'absolute', 'actions': {'joint_pos': np.full((50, 14), np.inf, np.float32)}},
    {'type': 'actions', 'space': 'joint_pos', 'mode': 'absolute', 'actions': {'joint_pos': np.zeros((50, 14), np.int32)}},
    {'type': 'actions', 'space': 'joint_pos', 'mode': 'absolute', 'actions': {'joint_pos': [[0.0] * 14] * 50}},
    {'type': 'actions', 'space': 'joint_pos', 'mode': 'absolute', 'actions': {'joint_pos': {
        b'nd': True, b'type': '<f4', b'kind': b'', b'shape': [50, 14], b'data': bytes(2800)}}},
    {'type': 'actions', 'space': 'joint_pos', 'mode': 'absolute', 'actions': {'joint_pos': np.full((50, 14), 1e100, np.float64)}},
], ids=['stop', 'error', 'delta', 'shape', 'nan', 'inf', 'integer', 'list', 'legacy', 'float32-overflow'])
def test_bad_replies_latch_failure_without_replay_or_reset_recovery(tmp_path, body):
    with model_server() as server:
        server.responses.put({'body': body})
        model = backend(configured(tmp_path, settings(server.address)))
        try:
            model.load()
            with pytest.raises(RuntimeError) as error:
                model.predict(np.zeros(14, np.float32), images(), 'task')
            assert 'fixture-key' not in str(error.value)
            with pytest.raises(RuntimeError):
                model.reset_context()
            with pytest.raises(RuntimeError):
                model.predict(np.zeros(14, np.float32), images(), 'task')
            assert len(server.requests) == 1 and len(server.hellos) == 1
        finally:
            model.close()


def test_timeout_and_idle_disconnect_are_terminal(tmp_path):
    with model_server() as server:
        server.responses.put({'gate': threading.Event()})
        raw = settings(server.address)
        raw['execution'] = {'inference_timeout_s': .1}
        model = backend(configured(tmp_path, raw))
        try:
            model.load()
            started = time.monotonic()
            with pytest.raises(RuntimeError, match='timeout'):
                model.predict(np.zeros(14, np.float32), images(), 'task')
            assert time.monotonic() - started < 2
            with pytest.raises(RuntimeError):
                model.check_health()
        finally:
            model.close()
    with model_server() as server:
        model = backend(configured(tmp_path, settings(server.address)))
        try:
            model.load()
            next(iter(server.connections)).close(1011)
            def failed():
                try:
                    model.check_health()
                except RuntimeError:
                    return True
            wait(failed)
        finally:
            model.close()


def test_action_reply_above_old_limit_uses_diagnostic_four_mib_limit(tmp_path):
    with model_server() as server:
        server.responses.put({'body': {'type': 'actions', 'space': 'joint_pos', 'mode': 'absolute',
            'actions': {'joint_pos': np.ones((50, 14), np.float32)}, 'metadata': bytes(2 * 1024 * 1024)}})
        model = backend(configured(tmp_path, settings(server.address)))
        try:
            model.load()
            actual = model.predict(np.zeros(14, np.float32), images(), 'task')
            np.testing.assert_array_equal(actual, np.ones((50, 14), np.float32))
        finally:
            model.close()


def test_abort_discards_delayed_request_before_fresh_session(tmp_path):
    with model_server() as server:
        gate = threading.Event()
        server.responses.put({'gate': gate})
        model = backend(configured(tmp_path, settings(server.address)))
        with ThreadPoolExecutor(1) as pool:
            try:
                model.load()
                pending = pool.submit(model.predict, np.zeros(14, np.float32), images(), 'old')
                wait(lambda: len(server.requests) == 1)
                old_episode = server.requests[0]['episode_id']
                started = time.monotonic()
                model.abort()
                with pytest.raises(RuntimeError):
                    pending.result(2)
                assert time.monotonic() - started < 2
                wait(lambda: not server.ids)
                model.reset_context()
                gate.set()
                actual = model.predict(np.ones(14, np.float32), images(), 'new')
                np.testing.assert_array_equal(actual, np.ones((50, 14)))
                assert server.requests[-1]['episode_id'] != old_episode
                assert server.requests[-1]['seq'] == 0
            finally:
                gate.set()
                model.close()


def test_duplicate_robot_id_is_not_a_shared_connection(tmp_path):
    with model_server() as server:
        config = configured(tmp_path, settings(server.address))
        one, two = backend(config), backend(config)
        try:
            one.load()
            with pytest.raises(RuntimeError, match='4409'):
                two.load()
            one.predict(np.zeros(14, np.float32), images(), 'still working')
            assert len(server.hellos) == 2
        finally:
            two.close()
            one.close()


def test_connection_and_application_handshake_share_one_deadline(tmp_path):
    with model_server(hello_delay=.08, ready_delay=.08) as server:
        raw = settings(server.address)
        raw['service']['connect_timeout_s'] = .12
        model = backend(configured(tmp_path, raw))
        try:
            with pytest.raises(RuntimeError, match='timeout'):
                model.load()
            assert not server.requests
        finally:
            model.close()


@pytest.mark.parametrize('reply', [{'raw': 'text payload'}, {'raw': b'\xc1'}, {'close': 1011}])
def test_bad_frames_and_mid_request_disconnect_never_replay(tmp_path, reply):
    with model_server() as server:
        server.responses.put(reply)
        model = backend(configured(tmp_path, settings(server.address)))
        try:
            model.load()
            with pytest.raises(RuntimeError):
                model.predict(np.zeros(14, np.float32), images(), 'task')
            with pytest.raises(RuntimeError):
                model.predict(np.zeros(14, np.float32), images(), 'task')
            assert len(server.requests) == 1
        finally:
            model.close()
