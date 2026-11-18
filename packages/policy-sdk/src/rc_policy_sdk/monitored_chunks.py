"""Opt-in scalar telemetry for policies using the asynchronous chunk scheduler."""
import threading
import time

from .async_chunks import AsyncChunkPolicy


class MonitoredAsyncChunkPolicy(AsyncChunkPolicy):
    def __init__(self, backend, **settings):
        self._metrics_lock = threading.Lock()
        self._created_at = self._stage_at = time.monotonic()
        self._pending_at = self._last_finished = None
        self._stage_times = {}
        self._metrics = dict(context_state='initializing', model_stage='loading',
                             inference_count=0, inference_completed=0, inference_errors=0,
                             inference_busy=0, pending=0, reset_count=0, cached_actions=0,
                             consumed_actions=0, last_trim=0, trimmed_actions_total=0)
        super().__init__(backend, **settings)
        self._record(context_state='ready', task=self.instruction[:512], chunk_size=self.chunk_size,
                     request_when_remaining=self.request_when_remaining, inference_timeout_s=self.timeout)

    def _record(self, **values):
        with self._metrics_lock:
            self._metrics.update(values)

    def monitor(self):
        with self._metrics_lock:
            now = time.monotonic()
            snapshot = self._metrics.copy()
            snapshot['context_uptime_s'] = now - self._created_at
            snapshot['model_stage_age_ms'] = (now - self._stage_at) * 1000
            if self._pending_at is not None:
                snapshot['pending_age_ms'] = (now - self._pending_at) * 1000
                snapshot['pending_remaining_ms'] = max(0., self.timeout * 1000 - snapshot['pending_age_ms'])
            if self._last_finished is not None:
                snapshot['last_prediction_age_s'] = now - self._last_finished
            return snapshot

    def _load(self):
        started = time.monotonic()
        self._load_backend()
        warming = time.monotonic()
        self.backend.warmup()
        with self._metrics_lock:
            self._stage_at = time.monotonic()
            self._metrics.update(model_stage='idle', load_ms=(warming - started) * 1000,
                                 warmup_ms=(self._stage_at - warming) * 1000)

    def _submit_prediction(self, qpos, images, instruction):
        shapes = {name + '_image_shape': 'x'.join(map(str, frame.shape)) for name, frame in images.items()}
        with self._metrics_lock:
            future = self._executor.submit(self._timed_prediction, qpos, images, instruction)
            self._pending_at = self._deadline - self.timeout
            self._metrics['inference_count'] += 1
            self._metrics.update(shapes, pending=1, last_submission_actions=self._request[0])
        return future

    def prediction_stage(self, name):
        """Called by a backend at actual stage boundaries on the inference thread."""
        with self._metrics_lock:
            if self._metrics['inference_busy']:  # Warmup also calls the backend's predict().
                self._end_stage(name, time.monotonic())

    def _end_stage(self, next_stage, now):
        previous = self._metrics['model_stage']
        if previous != 'predict':
            self._stage_times[previous + '_ms'] = (now - self._stage_at) * 1000
        self._stage_at = now
        self._metrics['model_stage'] = next_stage

    def _timed_prediction(self, qpos, images, instruction):
        with self._metrics_lock:
            started = self._stage_at = time.monotonic()
            self._stage_times.clear()
            self._metrics.update(model_stage='predict', inference_busy=1)
        try:
            actions, finished = self._predict(qpos, images, instruction)
        except Exception as exc:
            with self._metrics_lock:
                self._metrics['inference_errors'] += 1
                self._metrics.update(inference_busy=0, last_error=str(exc)[:512],
                                     last_error_stage=self._metrics['model_stage'])
                self._end_stage('failed', time.monotonic())
            raise
        with self._metrics_lock:
            self._end_stage('idle', finished)
            self._last_finished = finished
            elapsed = (finished - started) * 1000
            count = self._metrics['inference_completed'] + 1
            avg = self._metrics.get('predict_avg_ms', 0.)
            self._metrics.update(self._stage_times, inference_busy=0, inference_completed=count,
                                 predict_ms=elapsed, predict_avg_ms=avg + (elapsed - avg) / count,
                                 predict_max_ms=max(elapsed, self._metrics.get('predict_max_ms', 0.)))
        return actions, finished

    def _collect(self, runtime):
        actions = super()._collect(runtime)
        with self._metrics_lock:
            self._pending_at = None
            self._metrics.update(pending=0, last_trim=self.last_trim, cached_actions=len(actions))
            self._metrics['trimmed_actions_total'] += self.last_trim
        return actions

    def step(self, state, action_chunk, images, *, runtime):
        self._record(consumed_actions=int(runtime.consumed_actions), control_epoch=int(runtime.control_epoch),
                     cached_actions=len(action_chunk))
        try:
            return super().step(state, action_chunk, images, runtime=runtime)
        except Exception as exc:
            self._record(context_state='failed', last_error=str(exc)[:512])
            raise

    def reset(self):
        super().reset()
        with self._metrics_lock:
            self._pending_at = None
            self._stage_at = time.monotonic()
            self._metrics['reset_count'] += 1
            self._metrics.update(context_state='ready', model_stage='idle', pending=0,
                                 consumed_actions=0, cached_actions=0, last_trim=0)
            for key in ('last_error', 'last_error_stage', 'control_epoch', 'last_submission_actions'):
                self._metrics.pop(key, None)

    def close(self):
        super().close()
        with self._metrics_lock:
            self._pending_at = None
            self._metrics.update(context_state='closed', pending=0)
