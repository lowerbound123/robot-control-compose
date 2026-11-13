"""Per-target callbacks and one GPU executor; RC alone consumes actions."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import copy
import json
import logging
from pathlib import Path
import threading
import time

import numpy as np
from rc_policy_sdk.transitions import TrajectoryAddTransition
from rc_policy_sdk.actions import normalize_actions

from .history import HistoryBuffer
from .configuration import DEFAULTS
from .settings import SNAPSHOT

CAMERAS = ('head', 'left_wrist', 'right_wrist')


def frame(value, name):
    value = np.asarray(value)
    if value.ndim != 3 or value.shape[2] != 3 or value.dtype != np.uint8 or not value.size:
        raise ValueError(f'{name} must be nonempty RGB uint8 HWC')
    return value


class HistoryPolicy:
    robot_type = 'arx_acone'
    default_gripper_threshold = -.8

    def __init__(self, config, *, policy_config=None, backend=None, project_root=None):
        self.config = copy.deepcopy(config)
        self.policy_config = policy_config if policy_config is not None else {}
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self.instruction = self._instruction()
        self.x = config['execution']['replan_every_actions']
        self.pipelined = config['execution']['pipelined']
        self.timeout = float(config['execution']['inference_timeout_s'])
        self.history_enabled = config['history']['enabled']
        self.transition = TrajectoryAddTransition(**config.get('transition', DEFAULTS['transition']))
        self.model_config_snapshot = copy.deepcopy(config)
        self._settings_token = None
        self.pending = self._request = None
        self._epoch = self._consumed = self._last_submission = None
        self._config_version = None
        self._history_lock = threading.RLock()
        self._history_origin = self._observed_consumed = None
        self._failure = None
        self._closed = False
        self.canonical = np.empty((0, 14), np.float32)
        self._metrics_lock = threading.Lock()
        self._created_at = time.monotonic()
        self._metrics = dict(context_state='initializing', robot_type=self.robot_type,
                             checkpoint=str(config.get('model', {}).get('checkpoint', 'injected backend')),
                             pipelined=int(self.pipelined), history_enabled=int(self.history_enabled),
                             replan_every_actions=self.x, inference_timeout_s=self.timeout,
                             history_tick=0, history_frames=0, context_slots=0,
                             inference_count=0, inference_completed=0, inference_errors=0, inference_busy=0,
                             reset_count=0, pending=0, last_trim=0, trimmed_actions_total=0, cached_actions=0,
                             consumed_actions=0, control_epoch=-1, last_submission_actions=-1,
                             load_ms=0., warmup_ms=0., predict_ms=0., predict_avg_ms=0., predict_max_ms=0.,
                             last_error='', _pending_at=0., _deadline=0.,
                             _last_prediction_at=0.)
        if backend is None:
            from .backend import HistoryBackend
            backend = HistoryBackend(config, robot_type=self.robot_type)
        self.backend = backend
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='history-inference')
        try:
            self._executor.submit(self._load).result()
            if type(self.x) is not int or not 1 <= self.x <= backend.chunk_size:
                raise ValueError('replan_every_actions must be in [1, checkpoint.chunk_size]')
            self.history = HistoryBuffer(backend.history_slot_count, backend.history_slot_stride,
                                         backend.resize_history)
            self._record(context_state='ready', chunk_size=backend.chunk_size,
                         history_slot_count=backend.history_slot_count,
                         history_slot_stride=backend.history_slot_stride,
                         history_capacity_frames=self.history.frames.maxlen)
        except BaseException:
            self.close()
            raise

    def _load(self):
        try:
            started = time.perf_counter()
            self.backend.load()
            self._record(load_ms=(time.perf_counter() - started) * 1000)
            started = time.perf_counter()
            self.backend.warmup()
            self._record(warmup_ms=(time.perf_counter() - started) * 1000)
        except Exception:
            snapshot = self._backend_snapshot()
            logging.getLogger(__name__).error(
                'history initialization failed: stage=%s', snapshot.get('model_stage', 'unavailable'))
            raise

    def _backend_snapshot(self):
        result = {}
        try:
            callback = getattr(self.backend, 'monitor', None)
            if callable(callback):
                result.update(callback())
        except Exception as exc:
            result['backend_monitor_error'] = f'{type(exc).__name__}: {exc}'[:256]
        return result

    def _instruction(self):
        override = self.policy_config.get('params', {}).get('task')
        return self.config['runtime']['instruction'] if override is None else override

    def _record(self, **values):
        with self._metrics_lock:
            self._metrics.update(values)

    def monitor(self):
        now = time.monotonic()
        with self._metrics_lock:
            snapshot = dict(self._metrics)
        pending_at, deadline = snapshot.pop('_pending_at'), snapshot.pop('_deadline')
        finished = snapshot.pop('_last_prediction_at')
        snapshot.update(context_uptime_s=max(0., now-self._created_at), task=self._instruction()[:512],
                        pending_age_ms=max(0., (now-pending_at)*1000) if pending_at else 0.,
                        pending_remaining_ms=max(0., (deadline-now)*1000) if deadline else 0.,
                        last_prediction_age_s=max(0., now-finished) if finished else -1.)
        result = {'subtask': '', **self._backend_snapshot(), **snapshot}
        result['request_when_remaining'] = self.backend.chunk_size - self.x
        if 'Subtask' not in self.config.get('inference', {}).get('generate_language', []):
            result['subtask'] = ''
        return result

    def _refresh_settings(self):
        raw = self.policy_config.get('params', {}).get(SNAPSHOT)
        if raw is None or raw == self._settings_token:
            return
        token = raw
        raw = json.loads(raw)
        # Model/processor changes use reinitialize; these leaves only change
        # execution at a committed callback/reset boundary, never reread disk.
        for section in ('execution', 'runtime', 'history', 'transition'):
            self.config[section] = copy.deepcopy(raw[section])
        self.model_config_snapshot = copy.deepcopy(raw)
        self._settings_token = token
        self.x = raw['execution']['replan_every_actions']
        self.pipelined = raw['execution']['pipelined']
        self.timeout = float(raw['execution']['inference_timeout_s'])
        self.history_enabled = raw['history']['enabled']
        self.transition = TrajectoryAddTransition(**raw['transition'])
        self._record(replan_every_actions=self.x, pipelined=int(self.pipelined),
                     inference_timeout_s=self.timeout, history_enabled=int(self.history_enabled))

    def _predict(self, qpos, images, instruction, contexts):
        started = time.monotonic()
        self._record(inference_busy=1)
        try:
            actions, timings = self.backend.predict(qpos, images, instruction, contexts)
            actions = np.asarray(actions, dtype=np.float32)
            if actions.shape != (self.backend.chunk_size, 14) or not np.isfinite(actions).all():
                raise ValueError('model must return a finite [chunk_size,14] action chunk')
            timings = {str(k): float(v) for k, v in timings.items()}
            if not all(np.isfinite(v) and v >= 0 for v in timings.values()):
                raise ValueError('model stage timings must be finite and nonnegative')
            finished = time.monotonic()
            elapsed = (finished-started)*1000
            with self._metrics_lock:
                count = self._metrics['inference_completed'] + 1
                average = self._metrics['predict_avg_ms']
                self._metrics.update(timings, inference_completed=count, predict_ms=elapsed,
                                     predict_avg_ms=average+(elapsed-average)/count,
                                     predict_max_ms=max(elapsed,self._metrics['predict_max_ms']),
                                     _last_prediction_at=finished)
            return actions.copy(), time.monotonic()
        except Exception as exc:
            with self._metrics_lock:
                self._metrics['inference_errors'] += 1
                self._metrics['last_error'] = f'{type(exc).__name__}: {exc}'[:512]
            raise
        finally:
            self._record(inference_busy=0)

    def _launch(self, state, images, count, epoch, contexts):
        qpos = np.asarray(state['qpos'], dtype=np.float32)
        if qpos.shape != (14,) or not np.isfinite(qpos).all():
            raise ValueError('state.qpos must be finite with shape (14,)')
        snapshot = {name: frame(images[name], name).copy() for name in CAMERAS}
        now = time.monotonic()
        self._request = (count, epoch, now + self.timeout)
        self._last_submission = count
        with self._metrics_lock:
            self._metrics.update(pending=1, context_slots=len(contexts), _pending_at=now,
                                 _deadline=now+self.timeout, last_submission_actions=count,
                                 inference_count=self._metrics['inference_count']+1,
                                 **{name+'_image_shape': 'x'.join(map(str, image.shape))
                                    for name,image in snapshot.items()})
        self.pending = self._executor.submit(self._predict, qpos.copy(), snapshot, self.instruction, contexts)

    def _collect(self):
        _, _, deadline = self._request
        try:
            new, finished = self.pending.result(timeout=max(0., deadline-time.monotonic()))
        except FutureTimeoutError as exc:
            raise TimeoutError('model inference exceeded its submission deadline') from exc
        if finished > deadline:
            raise TimeoutError('model inference exceeded its submission deadline')
        self.pending = self._request = None
        self._record(pending=0, last_trim=0, _pending_at=0., _deadline=0.)
        # The response retains the request's time origin. Only RC knows how
        # many targets were actually consumed while this callback was running.
        return new

    @staticmethod
    def _process(actions, threshold=-.8):
        result = actions.copy()
        grippers = result[:, [6, 13]]
        result[:, [6, 13]] = np.where(grippers > threshold, .04, grippers)
        return result

    def _observe_history(self, images, runtime):
        """Called with the history lock; observations may overtake an infer RPC."""
        count, epoch = runtime.consumed_actions, runtime.control_epoch
        version = getattr(runtime, 'config_version', 1)
        if self._epoch is not None and (epoch < self._epoch or version < self._config_version):
            raise RuntimeError('history observation epoch/version moved backwards')
        self._epoch, self._config_version = epoch, version
        if self._observed_consumed is None:
            self._history_origin = self._observed_consumed = count
        elif count > self._observed_consumed:
            delta = count - self._observed_consumed
            if self.history_enabled:
                self.history.append(frame(images['head'], 'head'), count=delta)
                if delta > 1:
                    logging.getLogger(__name__).warning(
                        'consumed actions jumped from %d to %d; filling %d intermediate history '
                        'frames with current head observation', self._observed_consumed, count, delta-1)
            self._observed_consumed = count
        self._record(history_tick=self.history.tick, history_frames=len(self.history.frames),
                     consumed_actions=self._observed_consumed, control_epoch=epoch)

    def observe(self, state, images, *, runtime):
        if self._closed or self._failure is not None:
            return
        with self._history_lock:
            self._refresh_settings()
            self._observe_history(images, runtime)

    def step(self, state, action_chunk, images, *, runtime):
        if self._closed:
            raise RuntimeError('policy is closed')
        if self._failure is not None:
            raise self._failure
        try:
            with self._history_lock:
                self._refresh_settings()
            instruction = self._instruction()
            if instruction != self.instruction:
                self.reset()
                action_chunk = []
            old = normalize_actions(action_chunk, 'qpos', allow_empty=True).actions
            count, epoch = runtime.consumed_actions, runtime.control_epoch
            with self._history_lock:
                if self._consumed is not None and count < self._consumed:
                    raise RuntimeError('consumed action counter moved backwards')
                self._observe_history(images, runtime)
                self._consumed = count
                tick = max(0, count-self._history_origin)
                contexts = self.history.snapshot(tick=tick) if self.history_enabled else ()
            if len(old) > self.backend.chunk_size - self.x:
                self.canonical = old.copy()
                self._record(cached_actions=len(old))
                return old.copy()
            self._launch(state, images, count, epoch, contexts)
            new = self._collect()
            self.canonical = self.transition.apply(old, new) if self.pipelined else new.copy()
            self._record(cached_actions=len(self.canonical))
            threshold = self.policy_config.get('params', {}).get('gripper_threshold', self.default_gripper_threshold)
            return self._process(self.canonical, threshold=threshold)
        except Exception as exc:
            self._failure = exc
            self._record(context_state='failed', last_error=f'{type(exc).__name__}: {exc}'[:512])
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
        self._refresh_settings()
        self.instruction = self._instruction()
        self._executor.submit(self.backend.reset_context).result()
        with self._history_lock:
            self.history.reset()
            self._history_origin = self._observed_consumed = None
            self._epoch = self._consumed = self._last_submission = None
            self._config_version = None
        self.canonical = np.empty((0, 14), np.float32)
        with self._metrics_lock:
            self._metrics.update(history_tick=0, history_frames=0, context_slots=0,
                                 pending=0, last_trim=0, cached_actions=0, consumed_actions=0, control_epoch=-1,
                                 last_submission_actions=-1, _pending_at=0., _deadline=0.,
                                 reset_count=self._metrics['reset_count']+1)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._record(context_state='closed')
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
        load_model_config(path, project_root=self.project_root, robot_type=self.robot_type)
        try:
            selected = path.relative_to(self.project_root).as_posix()
        except ValueError:
            selected = str(path)
        patch = {'params.model_config_path': selected}
        if SNAPSHOT in self.policy_config.get('params', {}):
            patch['params.' + SNAPSHOT] = None
        return patch
