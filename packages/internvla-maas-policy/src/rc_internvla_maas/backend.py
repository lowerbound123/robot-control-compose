"""One bounded, serial MaaS session on an owned background event loop."""
import asyncio
from concurrent.futures import CancelledError, TimeoutError as FutureTimeout
import copy
import logging
import math
from numbers import Real
import threading
import time
from time import monotonic
from urllib.parse import quote, urlencode
import uuid

import numpy as np
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .codec import MAX_MESSAGE_BYTES, pack, unpack
from .images import encode_jpeg, resize_rgb
from .metrics import RequestMetrics, StageTimings

CAMERAS = {'head': 'front', 'left_wrist': 'wrist_left', 'right_wrist': 'wrist_right'}


class MaasError(RuntimeError):
    """A sanitized remote failure; recovery requires a new policy context."""


class MaasBackend:
    chunk_size = 50

    def __init__(self, config):
        self.config = copy.deepcopy(config)
        self.service = self.config['service']
        self._loop = self._thread = self._ws = self._active_task = None
        self._lock = threading.RLock()
        self._calls = threading.Lock()
        self._failure = None
        self._closed = self._interrupted = False
        self._seq = 0
        self._episode = None
        self._metrics = RequestMetrics()
        self._timings = StageTimings(
            ('dispatch', 'prepare', 'resize', 'encode', 'pack', 'send', 'response_wait', 'decode', 'validate', 'server'),
            last_source_image_bytes='—', last_image_bytes='—', last_request_bytes='—', last_response_bytes='—')
        # websockets DEBUG includes Authorization headers and payloads. Never
        # propagate its transport logger into the worker's captured root logger.
        self._logger = logging.Logger('internvla-maas.transport', logging.CRITICAL + 1)
        self._logger.addHandler(logging.NullHandler())
        self._logger.propagate = False

    def _redact(self, value):
        text = str(value).replace(self.service['api_key'], '[REDACTED]')
        return ''.join(c if c.isprintable() else ' ' for c in text)[:300]

    def _fault(self, error):
        if isinstance(error, MaasError):
            message = self._redact(error)
        elif isinstance(error, InvalidStatus):
            message = f'MaaS upgrade rejected (HTTP {error.response.status_code})'
        elif isinstance(error, ConnectionClosed):
            frame = error.rcvd or error.sent
            code = frame.code if frame is not None else 1006
            message = f'MaaS connection closed ({code}); cold reload required'
        elif isinstance(error, TimeoutError):
            message = 'MaaS request timeout; cold reload required'
        elif isinstance(error, (ValueError, TypeError, KeyError, OverflowError, FloatingPointError)):
            message = 'invalid MaaS protocol data; cold reload required'
        else:
            message = f'MaaS transport failed ({type(error).__name__}); cold reload required'
        with self._lock:
            if self._failure is None:
                self._failure = message
            return MaasError(self._failure)

    def check_health(self):
        with self._lock:
            if self._failure is not None:
                raise MaasError(self._failure)
            if self._closed:
                raise MaasError('MaaS backend is closed')

    def monitor(self):
        return {**self._metrics.snapshot(), **self._timings.snapshot()}

    def _serve(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    def load(self):
        self.check_health()
        if self._loop is not None:
            raise MaasError('MaaS backend is already loaded')
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, name='maas-network', daemon=True)
        self._thread.start()
        self._call(self._connect, self.service['connect_timeout_s'])

    def warmup(self):
        self.check_health()  # No fabricated observations or model requests.

    async def _disconnect(self):
        socket, self._ws = self._ws, None  # Fence the health watcher before closing.
        if socket is not None:
            await socket.close()

    async def _watch(self, socket):
        await socket.wait_closed()
        with self._lock:
            if socket is self._ws and not self._interrupted and not self._closed:
                code = socket.close_code or 1006
                self._fault(MaasError(f'MaaS connection closed ({code}); cold reload required'))

    async def _dispatch(self, function, timeout, *, prediction=False, queued_at=None):
        with self._lock:
            if prediction and self._interrupted:
                raise asyncio.CancelledError()
        if prediction and queued_at is not None:
            self._timings.record('dispatch', (monotonic() - queued_at) * 1000.)
        self._active_task = asyncio.current_task()
        try:
            async with asyncio.timeout(timeout):
                result = await function()
            if prediction:
                self._metrics.finish(failed=False)
            return result
        except asyncio.CancelledError:
            if prediction:
                self._metrics.cancel()
            await self._disconnect()
            raise
        except Exception as error:
            if prediction:
                self._metrics.finish(failed=True)
            fault = self._fault(error)
            await self._disconnect()
            raise fault from None
        finally:
            self._active_task = None

    def _call(self, function, timeout, *, prediction=False):
        with self._calls:
            self.check_health()
            if self._loop is None:
                raise MaasError('MaaS backend is not loaded')
            queued_at = monotonic() if prediction else None
            future = asyncio.run_coroutine_threadsafe(
                self._dispatch(function, timeout, prediction=prediction, queued_at=queued_at), self._loop)
            try:
                return future.result(timeout + self.service['close_timeout_s'] + 2)
            except CancelledError:
                raise MaasError('MaaS request cancelled by lifecycle') from None
            except FutureTimeout:
                if prediction:
                    self._metrics.finish(failed=True)
                self.abort()
                raise self._fault(TimeoutError()) from None

    async def _receive(self, *, timed=False):
        if timed:
            with self._timings.measure('response_wait'):
                frame = await self._ws.recv()
            self._timings.set_values(last_response_bytes=len(frame) if isinstance(frame, bytes) else '—')
            self._timings.missing('server')
            with self._timings.measure('decode'):
                message = unpack(frame)
            server_ms = message.get('server_ms')
            if isinstance(server_ms, Real) and not isinstance(server_ms, bool):
                self._timings.record('server', float(server_ms))
        else:
            message = unpack(await self._ws.recv())
        if message['type'] in ('stop', 'error'):
            details = self._redact(message.get('reason', message.get('message', '')))
            code = self._redact(message.get('code', ''))
            raise MaasError(f'MaaS {message["type"]} {code}: {details}; cold reload required')
        return message

    async def _connect(self):
        address = (self.service['address'].rstrip('/') + '/v1/robot/'
                   + quote(self.service['model'], safe='') + '/act?'
                   + urlencode({'robot_id': self.service['robot_id']}))
        self._ws = await connect(address, additional_headers={
            'Authorization': 'Api-Key ' + self.service['api_key']},
            open_timeout=self.service['connect_timeout_s'], close_timeout=self.service['close_timeout_s'],
            compression=None, proxy=None, max_size=MAX_MESSAGE_BYTES, logger=self._logger)
        hello = await self._receive()
        if (hello['type'] != 'server_hello' or hello.get('protocol') != 'maas-robot/1'
                or hello.get('model') != self.service['model']):
            raise MaasError('incompatible MaaS server_hello protocol or model')
        requires, provides = hello.get('requires'), hello.get('provides')
        if not isinstance(requires, dict) or not isinstance(provides, dict):
            raise MaasError('MaaS server_hello requires capabilities')
        image_roles = requires.get('images')
        if (not isinstance(image_roles, dict) or set(image_roles) != set(CAMERAS.values())
                or requires.get('state') != ['joint_pos']):
            raise MaasError('incompatible MaaS camera roles or state fields')
        for size in image_roles.values():
            if (not isinstance(size, (tuple, list)) or len(size) != 2
                    or any(type(n) is not int or n <= 0 for n in size)):
                raise MaasError('invalid MaaS image dimensions')
        hz = provides.get('control_hz')
        if (provides.get('space') != 'joint_pos' or provides.get('fields') != ['joint_pos']
                or type(provides.get('horizon')) is not int or provides['horizon'] != 50
                or type(hz) not in (int, float) or not math.isfinite(hz)
                or not math.isclose(hz, self.config['execution']['expected_control_hz'], rel_tol=1e-6)):
            raise MaasError('incompatible MaaS action space, horizon or control_hz')
        encodings = ['raw', 'jpeg'] if self.config['observations']['jpeg'] else ['raw']
        await self._ws.send(pack(dict(type='robot_hello', robot_id=self.service['robot_id'],
            embodiment=self.service['embodiment'], dof=14, images=list(CAMERAS.values()),
            state=['joint_pos'], action_spaces=['joint_pos'], image_encodings=encodings)))
        ready = await self._receive()
        if ready['type'] != 'ready' or not isinstance(ready.get('session_id'), str) or not ready['session_id']:
            raise MaasError('MaaS handshake did not return ready with a session_id')
        self._seq, self._episode = 0, uuid.uuid4().hex
        with self._lock:
            self._interrupted = False
        asyncio.create_task(self._watch(self._ws))

    def predict(self, qpos, images, instruction):
        async def request():
            with self._timings.measure('prepare'):
                if self._ws is None:
                    raise MaasError('MaaS session is unavailable; cold reload required')
                state = np.asarray(qpos, dtype=np.float32).copy()
                if state.shape != (14,) or not np.isfinite(state).all():
                    raise MaasError('MaaS state must be finite float32[14]')
                with np.errstate(over='raise', invalid='raise'):
                    for index, offset in self.config['observations']['state_offsets'].items():
                        state[int(index)] += offset
                frames = {}
                for name, role in CAMERAS.items():
                    frame = np.asarray(images[name])
                    if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8 or not frame.size:
                        raise MaasError('MaaS images must be nonempty RGB uint8 HWC')
                    frames[role] = frame
                source_bytes = sum(frame.nbytes for frame in frames.values())
                with self._timings.measure('resize'):
                    frames = {role: resize_rgb(frame) for role, frame in frames.items()}
                observations = self.config['observations']
                if observations['jpeg']:
                    with self._timings.measure('encode'):
                        frames = {role: {'enc': 'jpeg', 'data': encode_jpeg(frame, observations['jpeg_quality'])}
                                  for role, frame in frames.items()}
                    image_bytes = sum(len(frame['data']) for frame in frames.values())
                else:
                    image_bytes = sum(frame.nbytes for frame in frames.values())
                self._timings.set_values(last_source_image_bytes=source_bytes,
                                        last_image_bytes=image_bytes)
                message = dict(type='obs', seq=self._seq, t=time.time(), episode_id=self._episode,
                               instruction=instruction, images=frames, state={'joint_pos': state})
                self._seq += 1
            with self._timings.measure('pack'):
                payload = pack(message)
            self._timings.set_values(last_request_bytes=len(payload))
            self._metrics.start()
            with self._timings.measure('send'):
                await self._ws.send(payload)
            reply = await self._receive(timed=True)
            with self._timings.measure('validate'):
                if (reply['type'] != 'actions' or reply.get('space') != 'joint_pos'
                        or reply.get('mode') != 'absolute' or not isinstance(reply.get('actions'), dict)):
                    raise MaasError('MaaS must return absolute joint_pos actions')
                actions = reply['actions'].get('joint_pos')
                if (not isinstance(actions, np.ndarray) or actions.shape != (50, 14)
                        or actions.dtype.kind != 'f' or not np.isfinite(actions).all()):
                    raise MaasError('MaaS actions must be finite floating point [50,14]')
                with np.errstate(over='raise', invalid='raise'):
                    result = actions.astype(np.float32, copy=True)
            return result
        return self._call(request, self.config['execution']['inference_timeout_s'], prediction=True)

    async def _interrupt(self):
        task = self._active_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._disconnect()

    def abort(self):
        """Fence queued predictions and drain the active transport before SDK drain."""
        with self._lock:
            self._interrupted = True
            loop = self._loop
        if loop is not None and not loop.is_closed():
            future = asyncio.run_coroutine_threadsafe(self._interrupt(), loop)
            future.result(self.service['close_timeout_s'] + 2)

    def reset_context(self):
        self.check_health()  # Never clear a transport/protocol fault on ordinary reset.
        self.abort()
        self._call(self._connect, self.service['connect_timeout_s'])

    def close(self):
        if self._closed:
            return
        try:
            self.abort()
        finally:
            with self._lock:
                self._closed = True
            if self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(self.service['close_timeout_s'] + 2)
                if self._thread.is_alive():
                    raise MaasError('MaaS network thread did not exit')
