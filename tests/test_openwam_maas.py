"""OpenWAM protocol v2 and chunk boundaries through both production entry points."""
import base64
from contextlib import contextmanager
import importlib.util
from io import BytesIO
import json
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from config_helpers import read_config

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ('openwam_maas_acone', 'openwam_maas_lift2')
CAMERAS = ('head', 'left_wrist', 'right_wrist')
PONG = dict(type='pong', protocol_version=2, representation='qpos', action_representation='chunk_delta',
            action_horizon=32, gripper_indices=[6, 13], multiview=True)


def entry(source):
    path = ROOT / 'policy' / source / 'main.py'
    spec = importlib.util.spec_from_file_location(source, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest(source):
    return read_config(ROOT / 'policy' / source / 'policy.yaml.example')


def frames():
    return {name: np.arange(i * 4 * 3, dtype=np.uint8).reshape(i, 4, 3) + i
            for i, name in enumerate(CAMERAS, 2)}


def response(offset=0):
    actions = np.arange(32, dtype=np.float32)[:, None] + np.ones((32, 14), np.float32) * offset
    actions[:3, 6] = [-1.8, -1.7, -1.6]
    actions[:3, 13] = [2.4, 2.5, 2.6]
    return dict(type='action_chunk', horizon=32, actions=actions.tolist())


@contextmanager
def peer(pong=None):
    pytest.importorskip('websockets.sync.server')
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.server import serve
    messages, replies = Queue(), Queue()
    gate = Event()
    def handle(socket):
        try:
            for raw in socket:
                message = json.loads(raw)
                messages.put((socket.id, message))
                if message['type'] == 'ping':
                    reply = PONG if pong is None else pong
                else:
                    try:
                        reply = replies.get_nowait()
                    except Empty:
                        reply = response()
                    if reply == 'wait':
                        gate.wait(5)
                        reply = response(100)
                    if reply == 'disconnect':
                        socket.close()
                        return
                socket.send(json.dumps(reply))
        except ConnectionClosed:
            pass
    with serve(handle, '127.0.0.1', 0, compression=None, close_timeout=.2) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield SimpleNamespace(url=f'ws://127.0.0.1:{server.socket.getsockname()[1]}',
                                  messages=messages, replies=replies, gate=gate)
        finally:
            gate.set()
            server.shutdown()
            thread.join(3)


@pytest.fixture(params=SOURCES)
def source(request):
    return request.param


def setup(source, server, **params):
    cfg = manifest(source)
    cfg['params'].update(service_url=server.url, **params)
    module = entry(source)
    ctx = module.initialize(cfg, runtime=None)
    connection, ping = server.messages.get(timeout=2)
    assert ping == {'type': 'ping'}
    return module, cfg, ctx, connection


def step(ctx, count, old, *, epoch=1, version=1):
    return ctx.step({'qpos': list(range(count + 1, count + 15))}, old, frames(),
                    runtime=SimpleNamespace(control_epoch=epoch, consumed_actions=count, config_version=version))


def test_default_exhausts_chunk_before_sending_fresh_observation(source):
    with peer() as server:
        module, cfg, ctx, connection = setup(source, server)
        try:
            assert cfg['execution']['mode'] == 'sync' and cfg['execution']['call_every_actions'] == 1
            assert cfg['params']['request_when_remaining'] == ctx.monitor()['request_when_remaining'] == 0
            actions = step(ctx, 0, [])
            sender, message = server.messages.get(timeout=2)
            assert sender == connection and message['type'] == 'chunk_obs'
            assert message['state'] == list(range(1, 15)) and message['prompt'] == cfg['params']['task']
            assert set(message['images']) == {name + '_camera' for name in CAMERAS}
            for name, frame in frames().items():
                with Image.open(BytesIO(base64.b64decode(message['images'][name + '_camera']))) as image:
                    assert image.format == 'PNG' and image.mode == 'RGB'
                    np.testing.assert_array_equal(np.asarray(image), frame)
            np.testing.assert_array_equal(actions[:, 0], np.arange(32))
            if source == 'openwam_maas_acone':
                np.testing.assert_allclose(actions[:3, 6], [-1.8, -1.7, .04])
                np.testing.assert_allclose(actions[:3, 13], .04)
            else:
                np.testing.assert_allclose(actions[:3, 6], 0)
                np.testing.assert_allclose(actions[:3, 13], [0, 4.9, 4.9])
            for count in range(1, 32):
                np.testing.assert_array_equal(step(ctx, count, actions[count:]), actions[count:])
            assert ctx.monitor()['inference_count'] == 1 and server.messages.empty()
            step(ctx, 32, [])
            assert server.messages.get(timeout=2)[1]['state'] == list(range(33, 47))
            assert ctx.monitor()['inference_count'] == 2
        finally:
            module.release(ctx)


def test_prefetch_boundary_and_latency_trim(source):
    from rc_policy_sdk.transitions import ClosestPointOverlapTransition
    with peer() as server:
        module, cfg, ctx, _ = setup(source, server, request_when_remaining=4)
        try:
            old = step(ctx, 0, [])
            server.messages.get(timeout=2)
            step(ctx, 27, old[27:])
            assert ctx.monitor()['inference_count'] == 1
            server.replies.put('wait')
            step(ctx, 28, old[28:])
            assert server.messages.get(timeout=2)[1]['state'][0] == 29
            np.testing.assert_array_equal(step(ctx, 29, old[29:]), old[29:])
            assert ctx.monitor()['inference_count'] == 2
            server.gate.set()
            new = ctx.pending.result(timeout=2)[0]
            merged = step(ctx, 30, old[30:])
            expected = ClosestPointOverlapTransition().apply(old[30:], new[2:])
            np.testing.assert_allclose(merged, expected)
            assert ctx.monitor()['last_trim'] == 2
        finally:
            module.release(ctx)


def test_task_switch_discards_inflight_and_isolates_other_connection(source):
    with peer() as server:
        module, cfg, ctx, first = setup(source, server, request_when_remaining=1)
        other = None
        try:
            _, _, other, second = setup(source, server)
            assert first != second
            old = step(ctx, 0, [])
            server.messages.get(timeout=2)
            server.replies.put('wait')
            step(ctx, 31, old[31:])
            assert server.messages.get(timeout=2)[0] == first
            patch = module.switch_task(ctx, 'new task')
            assert patch == {'params.task': 'new task'}
            cfg['params']['task'] = patch['params.task']
            new = step(ctx, 31, old[31:], epoch=2, version=2)
            assert new.shape == (32, 14) and new[0, 0] == 0
            _, handshake = server.messages.get(timeout=2)
            assert handshake == {'type': 'ping'}
            assert server.messages.get(timeout=2)[1]['prompt'] == 'new task'
            module.release(ctx)
            step(other, 0, [])
            sender, obs = server.messages.get(timeout=2)
            assert sender == second and obs['type'] == 'chunk_obs' and server.messages.empty()
        finally:
            server.gate.set()
            module.release(ctx)
            module.release(other)


@pytest.mark.parametrize('field,value', [('multiview', False), ('protocol_version', 1),
                                         ('action_representation', 'absolute'), ('action_horizon', 16),
                                         ('gripper_indices', [0, 7]), ('representation', 'epos')])
def test_invalid_handshake(source, field, value):
    with peer({**PONG, field: value}) as server, pytest.raises(ValueError, match=field):
        setup(source, server)


@pytest.mark.parametrize('reply', [dict(type='error', code='obs_validation_error'),
                                    {**response(), 'horizon': 31},
                                    {**response(), 'actions': [[0] * 13] * 32},
                                    {**response(), 'actions': [[float('nan')] * 14] * 32},
                                    {**response(), 'actions': [['0'] * 14] * 32},
                                    'disconnect', 'wait'], ids=['error', 'horizon', 'shape', 'nan', 'strings',
                                                                'disconnect', 'timeout'])
def test_bad_reply_faults_without_resending(source, reply):
    with peer() as server:
        module, cfg, ctx, _ = setup(source, server, inference_timeout_s=.3)
        try:
            server.replies.put(reply)
            from websockets.exceptions import ConnectionClosed
            failures = (RuntimeError, ValueError, ConnectionClosed, TimeoutError)
            with pytest.raises(failures) as fault:
                step(ctx, 0, [])
            server.messages.get(timeout=2)
            cfg['params']['task'] = 'updated after fault'
            with pytest.raises(type(fault.value)):
                step(ctx, 0, [], epoch=2, version=2)
            assert server.messages.empty()
        finally:
            server.gate.set()
            module.release(ctx)


def test_openwam_maas_lift2_configures_signed_gripper_offsets_once():
    with peer() as server:
        module, cfg, ctx, _ = setup('openwam_maas_lift2', server)
        try:
            module.configure(ctx, cfg, 'params.gripper_upper_value', '"+0.5"', lambda action: None)
            module.reset_context(ctx)
            actions = step(ctx, 0, [])
            assert actions[1, 13] == 3
            assert step(ctx, 1, actions[1:])[0, 13] == 3
        finally:
            module.release(ctx)


def test_real_worker_releases_shared_images_and_keeps_socket_on_idle_reset(tmp_path, monkeypatch, source):
    import os
    from host_helpers import LocalPolicyProcess
    from test_example_worker import command_result
    from project_templates import copy_templates
    from rc_policy_sdk.images import ImagePool
    from test_policy_monitor_worker import wait_until

    path = copy_templates(ROOT / 'policy' / source, tmp_path / source)
    roots = [ROOT / 'packages/policy-sdk/src', ROOT / 'policy' / source / 'src', ROOT / 'src']
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join(map(str, roots)))
    with peer() as server:
        cfg = manifest(source)
        cfg['params']['service_url'] = server.url
        worker = LocalPolicyProcess(path, cfg, init_timeout_s=6, inference_timeout_s=4, release_timeout_s=4)
        pool = ImagePool(tmp_path / 'frames', cameras=CAMERAS)
        for name, frame in frames().items():
            pool.publish(name, frame, 1.)
        lease = pool.acquire()
        try:
            worker.start()
            connection, ping = server.messages.get(timeout=2)
            assert ping == {'type': 'ping'}
            def monitor(sequence):
                assert worker.supports_monitor and worker.submit_monitor(sequence)
                message = wait_until(worker.poll_monitor, 2)
                assert message['type'] == 'monitor_result', message
                return message['result']

            assert monitor(1)['protocol_version'] == 2
            for generation in (1, 2):
                if generation == 1:
                    server.replies.put('wait')
                worker.submit(dict(request_id=generation, generation=generation, config_version=generation,
                                   state={'qpos': [5.] * 14}, action_chunk=[], images=lease.descriptors,
                                   runtime=dict(control_epoch=generation, consumed_actions=0)))
                sender, obs = server.messages.get(timeout=2)
                if generation == 1:
                    try:
                        busy = monitor(2)
                        assert busy['inference_busy'] == busy['pending'] == 1 and worker.poll() is None
                        assert busy['model_stage'] == 'wait_response'
                    finally:
                        server.gate.set()
                result = wait_until(worker.poll, 4)
                assert result['type'] == 'result' and result['images_released'] is True, result
                actions = np.asarray(result['actions'])
                assert actions.shape == (32, 14)
                assert actions[1, 0] == (101 if generation == 1 else 1)
                ready = monitor(2 + generation)
                assert ready['pending'] == 0 and ready['last_request_bytes'] > 0
                np.testing.assert_array_equal(json.loads(ready['right_gripper_processed']), actions[:, 13])
                assert sender == connection and obs['type'] == 'chunk_obs'
                assert obs['prompt'] == (cfg['params']['task'] if generation == 1 else 'second task')
                worker.reset_context()
                if generation == 1:
                    worker.submit_command('task-change', 'switch-task', ['second task'])
                    patch = command_result(worker)['result']
                    assert patch == {'params.task': 'second task'}
                    worker.update_config(patch, 2)
        finally:
            forced = worker.close()
            assert worker.cleanup_confirmed and not worker.has_readers
            lease.release()
            pool.close()
        assert not forced and server.messages.empty()


def test_fully_consumed_prefetch_is_replaced_with_a_fresh_observation(source):
    with peer() as server:
        module, _, ctx, _ = setup(source, server, request_when_remaining=32)
        try:
            server.replies.put(response())
            server.replies.put('wait')
            step(ctx, 0, [])
            for _ in range(2):
                assert server.messages.get(timeout=2)[1]['state'][0] == 1
            server.gate.set()
            ctx.pending.result(timeout=2)
            server.replies.put(response(200))
            fresh = step(ctx, 32, [])
            assert fresh[0, 0] == 200 and server.messages.get(timeout=2)[1]['state'][0] == 33
        finally:
            server.gate.set()
            module.release(ctx)
