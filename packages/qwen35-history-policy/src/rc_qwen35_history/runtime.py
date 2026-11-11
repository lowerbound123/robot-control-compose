"""Per-target callbacks and one GPU executor; RC alone consumes actions."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import copy
import logging
from pathlib import Path
import threading
import time

import numpy as np
from rc_policy_sdk.transitions import TemporalTransition

from .history import HistoryBuffer

CAMERAS = ('head', 'left_wrist', 'right_wrist')


def frame(value, name):
    value = np.asarray(value)
    if value.ndim != 3 or value.shape[2] != 3 or value.dtype != np.uint8 or not value.size:
        raise ValueError(f'{name} must be nonempty RGB uint8 HWC')
    return value


class HistoryPolicy:
    def __init__(self, config, *, policy_config=None, backend=None, project_root=None):
        self.config = copy.deepcopy(config)
        self.policy_config = policy_config if policy_config is not None else {}
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self.instruction = self._instruction()
        self.x = config['execution']['replan_every_actions']
        self.pipelined = config['execution']['pipelined']
        self.timeout = float(config['execution']['inference_timeout_s'])
        self.history_enabled = config['history']['enabled']
        self.transition = TemporalTransition('exponential', None, 5., .25)
        self.pending = self._request = None
        self._epoch = self._consumed = self._last_submission = None
        self._failure = None
        self._closed = False
        self.canonical = np.empty((0, 14), np.float32)
        self._metrics_lock = threading.Lock()
        self._metrics = dict(history_tick=0, history_frames=0, context_slots=0, inference_count=0,
                             pending=0, last_trim=0, cached_actions=0)
        if backend is None:
            from .backend import HistoryBackend
            backend = HistoryBackend(config)
        self.backend = backend
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='history-inference')
        try:
            self._executor.submit(self._load).result()
            if type(self.x) is not int or not 1 <= self.x <= backend.chunk_size:
                raise ValueError('replan_every_actions must be in [1, checkpoint.chunk_size]')
            self.history = HistoryBuffer(backend.history_slot_count, backend.history_slot_stride,
                                         backend.resize_history)
        except BaseException:
            self.close()
            raise

    def _load(self):
        self.backend.load()
        self.backend.warmup()

    def _instruction(self):
        override = self.policy_config.get('params', {}).get('task')
        return self.config['runtime']['instruction'] if override is None else override

    def _record(self, **values):
        with self._metrics_lock:
            self._metrics.update(values)

    def monitor(self):
        with self._metrics_lock:
            return dict(self._metrics)

    def _predict(self, qpos, images, instruction, contexts):
        started = time.monotonic()
        actions, timings = self.backend.predict(qpos, images, instruction, contexts)
        actions = np.asarray(actions, dtype=np.float32)
        if actions.shape != (self.backend.chunk_size, 14) or not np.isfinite(actions).all():
            raise ValueError('model must return a finite [chunk_size,14] action chunk')
        timings = {str(k): float(v) for k, v in timings.items()}
        if not all(np.isfinite(v) and v >= 0 for v in timings.values()):
            raise ValueError('model stage timings must be finite and nonnegative')
        self._record(**timings, predict_ms=(time.monotonic()-started)*1000)
        return actions.copy(), time.monotonic()

    def _launch(self, state, images, count, epoch):
        qpos = np.asarray(state['qpos'], dtype=np.float32)
        if qpos.shape != (14,) or not np.isfinite(qpos).all():
            raise ValueError('state.qpos must be finite with shape (14,)')
        snapshot = {name: frame(images[name], name).copy() for name in CAMERAS}
        contexts = self.history.snapshot() if self.history_enabled else ()
        self._request = (count, epoch, time.monotonic() + self.timeout)
        self._last_submission = count
        self.pending = self._executor.submit(self._predict, qpos.copy(), snapshot, self.instruction, contexts)
        self._record(pending=1, context_slots=len(contexts), inference_count=self.monitor()['inference_count']+1)

    def _collect(self, count, epoch):
        submitted, request_epoch, deadline = self._request
        try:
            new, finished = self.pending.result(timeout=max(0., deadline-time.monotonic()))
        except FutureTimeoutError as exc:
            raise TimeoutError('model inference exceeded its submission deadline') from exc
        if finished > deadline:
            raise TimeoutError('model inference exceeded its submission deadline')
        if request_epoch != epoch:
            raise RuntimeError('stale inference epoch')
        stale = count-submitted
        if not 0 <= stale < len(new):
            raise RuntimeError('inference chunk is fully stale or counter moved backwards')
        self.pending = self._request = None
        if self.pipelined:
            self.canonical = self.transition.apply(self.canonical, new[stale:])
        else:
            self.canonical = new[:self.x].copy()
        self._record(pending=0, last_trim=stale)
        logging.getLogger(__name__).log(5, 'history prediction: consumed=%d trimmed=%d remaining=%d',
                                       count, stale, len(self.canonical))

    @staticmethod
    def _process(actions):
        result = actions.copy()
        grippers = result[:, [6, 13]]
        result[:, [6, 13]] = np.where(grippers > -.8, .04, grippers)
        return result

    def step(self, state, action_chunk, images, *, runtime):
        if self._closed:
            raise RuntimeError('policy is closed')
        if self._failure is not None:
            raise self._failure
        try:
            instruction = self._instruction()
            if instruction != self.instruction:
                self.reset()
                self.instruction = instruction
                action_chunk = []
            count, epoch = runtime.consumed_actions, runtime.control_epoch
            if self._epoch is None:
                self._epoch, self._consumed = epoch, count
            if epoch != self._epoch:
                raise RuntimeError('control epoch changed without reset_context')
            delta = count-self._consumed
            if delta < 0:
                raise RuntimeError('consumed action counter moved backwards')
            if delta:
                if self.history_enabled:
                    self.history.append(frame(images['head'], 'head'), count=delta)
                    if delta > 1:
                        logging.getLogger(__name__).warning(
                            'consumed actions jumped from %d to %d; filling %d intermediate history '
                            'frames with current head observation', self._consumed, count, delta - 1)
                self.canonical = self.canonical[delta:]
                self._consumed = count
            old = np.asarray(action_chunk, dtype=np.float32)
            if old.size == 0:
                old = np.empty((0, 14), np.float32)
            if old.shape != self.canonical.shape or not np.isfinite(old).all():
                raise RuntimeError('canonical mirror differs from RC remaining actions')
            if self.pending is not None and (self.pending.done() or not len(old)
                                             or time.monotonic() >= self._request[2]):
                self._collect(count, epoch)
            if self.pending is None:
                due = (not len(self.canonical) or
                       (self.pipelined and self._last_submission is not None
                        and count-self._last_submission >= self.x))
                if due:
                    self._launch(state, images, count, epoch)
                    if not len(self.canonical):
                        self._collect(count, epoch)
            self._record(history_tick=self.history.tick, history_frames=len(self.history.frames),
                         cached_actions=len(self.canonical))
            return self._process(self.canonical)
        except Exception as exc:
            self._failure = exc
            raise

    def _drain(self):
        if self.pending is not None:
            try:
                self.pending.result()
            except Exception:
                pass
        self.pending = self._request = None

    def reset(self):
        if self._closed:
            raise RuntimeError('policy is closed')
        if self._failure is not None:
            raise self._failure
        self._drain()
        self._executor.submit(self.backend.reset_context).result()
        self.history.reset()
        self.canonical = np.empty((0, 14), np.float32)
        self._epoch = self._consumed = self._last_submission = None
        self._record(history_tick=0, history_frames=0, context_slots=0, pending=0, last_trim=0, cached_actions=0)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._drain()
            self._executor.submit(self.backend.close).result()
            if hasattr(self, 'history'):
                self.history.reset()
        finally:
            # Existing host supervisor bounds stuck GPU/executor teardown.
            self._executor.shutdown(wait=True, cancel_futures=True)

    def switch_task(self, task):
        if not isinstance(task, str) or not task.strip():
            raise ValueError('task must be a nonempty string')
        return {'params.task': task}

    def switch_model(self, path):
        from .configuration import load_model_config, resolve_path
        path = resolve_path(path, self.project_root)
        load_model_config(path, project_root=self.project_root)
        return {'params.model_config_path': str(path)}
