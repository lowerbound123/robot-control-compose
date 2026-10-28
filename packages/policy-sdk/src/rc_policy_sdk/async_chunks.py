"""Model-independent Qwen35 chunk scheduling, alignment and temporal blending.

Backends run on one owned executor. Callbacks copy shared images before returning;
only the RC control thread installs returned actions and advances consumption.
"""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import math
import time

import numpy as np

from .transitions import TemporalTransition


class AsyncChunkPolicy:
    def __init__(self, backend, *, instruction, chunk_size, request_when_remaining,
                 inference_timeout_s):
        if type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError('chunk_size must be a positive integer')
        if (type(request_when_remaining) is not int
                or not 1 <= request_when_remaining <= chunk_size):
            raise ValueError('request_when_remaining must be in [1, chunk_size]')
        if (type(inference_timeout_s) not in (int, float)
                or not math.isfinite(inference_timeout_s) or inference_timeout_s <= 0):
            raise ValueError('inference_timeout_s must be positive and finite')
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError('instruction must be nonempty')
        self.instruction = instruction
        self.chunk_size = chunk_size
        self.request_when_remaining = request_when_remaining
        self.timeout = float(inference_timeout_s)
        self._closed = False
        self._failure = None
        self.pending = None
        self._request = None
        self._deadline = None
        self._epoch = None
        self._consumed = None
        self.last_trim = 0
        self.canonical = np.empty((0, 14), np.float32)
        self.transition = TemporalTransition('exponential', None, 5.0, 0.25)
        self.backend = backend
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='policy-inference')
        try:
            self._executor.submit(self._load).result()
        except BaseException:
            self.close()
            raise

    def _load(self):
        self.backend.load()
        if self.backend.chunk_size != self.chunk_size:
            raise ValueError(f'model chunk_size must be exactly {self.chunk_size}')
        self.backend.warmup()

    def _predict(self, qpos, images, instruction):
        result = np.asarray(self.backend.predict(qpos, images, instruction), dtype=np.float32)
        if result.shape != (self.chunk_size, 14) or not np.isfinite(result).all():
            raise ValueError(f'model must return finite canonical actions shaped ({self.chunk_size}, 14)')
        return result.copy(), time.monotonic()

    def _launch(self, state, images, runtime):
        qpos = np.asarray(state['qpos'], dtype=np.float32)
        if qpos.shape != (14,) or not np.isfinite(qpos).all():
            raise ValueError('state.qpos must be finite and shaped (14,)')
        snapshot = {}
        for name in ('head', 'left_wrist', 'right_wrist'):
            frame = np.asarray(images[name])
            if frame.ndim != 3 or frame.shape[2] != 3 or frame.dtype != np.uint8 or not frame.size:
                raise ValueError(f'{name} must be a nonempty uint8 RGB image')
            snapshot[name] = frame.copy()
        self._request = (runtime.consumed_actions, runtime.control_epoch)
        self._deadline = time.monotonic() + self.timeout
        self.pending = self._executor.submit(self._predict, qpos.copy(), snapshot,
                                             self.instruction)

    @staticmethod
    def _process(actions):
        return actions.copy()

    def _collect(self, runtime):
        future = self.pending
        try:
            new, completed = future.result(timeout=max(0.0, self._deadline - time.monotonic()))
            if completed > self._deadline:
                raise TimeoutError('model inference exceeded its submission deadline')
        except FutureTimeoutError as exc:
            self._failure = TimeoutError('model inference exceeded its submission deadline')
            raise self._failure from exc
        except Exception as exc:
            self._failure = exc
            raise
        count, epoch = self._request
        self.pending = None
        self._request = None
        if epoch != runtime.control_epoch:
            raise RuntimeError('stale model inference epoch')
        self.last_trim = runtime.consumed_actions - count
        if self.last_trim < 0 or self.last_trim >= len(new):
            raise RuntimeError('inference consumption exceeds predicted chunk')
        new = new[self.last_trim:]
        self.canonical = self.transition.apply(self.canonical, new)
        return self._process(self.canonical)

    def step(self, state, action_chunk, images, *, runtime):
        if self._closed:
            raise RuntimeError('policy is closed')
        if self._failure is not None:
            raise self._failure
        if self._epoch is None:
            self._epoch = runtime.control_epoch
            self._consumed = runtime.consumed_actions
        if runtime.control_epoch != self._epoch:
            raise RuntimeError('control epoch changed without reset_context')
        delta = runtime.consumed_actions - self._consumed
        if delta < 0:
            raise RuntimeError('consumed action counter moved backwards')
        self.canonical = self.canonical[delta:]
        self._consumed = runtime.consumed_actions
        old = np.asarray(action_chunk, dtype=np.float32).reshape((-1, 14))
        if len(old) != len(self.canonical):
            raise RuntimeError('canonical mirror differs from RC remaining actions')
        if not np.isfinite(old).all():
            raise ValueError('nonfinite remaining actions')
        if self.pending is not None:
            if self.pending.done() or not len(old) or time.monotonic() >= self._deadline:
                old = self._collect(runtime)
        elif not len(old):
            self._launch(state, images, runtime)
            old = self._collect(runtime)
        if len(old) < self.request_when_remaining and self.pending is None:
            self._launch(state, images, runtime)
        return old

    def _drain(self):
        if self.pending is not None:
            try:
                self.pending.result()
            except Exception:
                pass  # Old-epoch results/errors cannot become new actions.
            self.pending = None
        self._request = None

    def reset(self):
        if self._closed:
            raise RuntimeError('policy is closed')
        self._drain()
        self._executor.submit(self.backend.reset_context).result()
        self.canonical = np.empty((0, 14), np.float32)
        self._epoch = self._consumed = None
        self._failure = None
        self.last_trim = 0

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._drain()
            if self.backend is not None:
                self._executor.submit(self.backend.close).result()
        finally:
            # The RC supervisor bounds shutdown and kills a stuck worker process.
            self._executor.shutdown(wait=True, cancel_futures=True)
