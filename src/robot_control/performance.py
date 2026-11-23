"""DEBUG-only diagnostics, isolated from control and recorder locks."""
import itertools
import queue
import threading
import time

from rc_policy_sdk.performance import Trace, scalars
from .log_format import format_log_time


class PerformanceLogger:
    def __init__(self, level, run_id, sink=None, *, capacity=2048, ingress=None):
        self.enabled = str(level).upper() in ('DEBUG', 'TRACE')
        self.run_id, self.sink = run_id, sink
        self.ingress = ingress
        self._queue = queue.Queue(maxsize=capacity)
        self._serial = itertools.count(1)
        self._lock = threading.Lock()
        self._closed = False
        self._dropped = 0
        self._thread = None
        if self.enabled and ingress is None:
            self._thread = threading.Thread(target=self._drain, name='performance-log', daemon=True)
            self._thread.start()

    def trace(self, kind):
        return Trace.start(f'{self.run_id}:{kind}:{next(self._serial)}') if self.enabled else None

    def emit(self, stage, *, trace=None, **values):
        if not self.enabled:
            return
        entry = {**scalars(values), 'event': 'performance', 'level': 'debug',
                 'stage': stage, 'rc_run_id': self.run_id, 'timestamp_ns': time.time_ns(),
                 'monotonic_ns': time.monotonic_ns()}
        if trace is not None:
            entry['performance'] = trace.snapshot()
            entry['trace_id'] = trace.data['trace_id']
        if self.ingress is not None:
            # The recorder owns the nonblocking handoff and its shutdown fence.
            if self._closed: return
            try: self.ingress.performance_entry(entry)
            except Exception: pass  # Diagnostics never change control results.
            return
        with self._lock:
            if self._closed:
                return
            try:
                self._queue.put_nowait(entry)
            except queue.Full:
                self._dropped += 1

    def _write(self, entry):
        entry['time'] = format_log_time(entry['timestamp_ns']/1e9)
        entry['message'] = 'performance ' + entry['stage']
        entry['logged_monotonic_ns'] = time.monotonic_ns()
        try:
            accepted = self.sink(entry)
            if accepted is False:
                with self._lock:
                    self._dropped += 1 + entry.get('dropped_events', 0)
        except Exception:
            with self._lock:
                self._dropped += 1 + entry.get('dropped_events', 0)

    def _drain(self):
        while True:
            try:
                entry = self._queue.get(timeout=.02)
            except queue.Empty:
                entry = None
            with self._lock:
                dropped, self._dropped = self._dropped, 0
                done = self._closed and self._queue.empty()
            if entry is not None:
                if dropped:
                    entry['dropped_events'] = dropped
                self._write(entry)
            elif dropped:
                self._write({'event': 'performance', 'level': 'debug', 'stage': 'diagnostic_gap',
                             'rc_run_id': self.run_id, 'timestamp_ns': time.time_ns(),
                             'monotonic_ns': time.monotonic_ns(), 'dropped_events': dropped})
            if done:
                return

    def close(self, deadline):
        with self._lock:
            self._closed = True
        if self._thread is not None:
            self._thread.join(max(0., deadline-time.monotonic()))
            return not self._thread.is_alive()
        return True
