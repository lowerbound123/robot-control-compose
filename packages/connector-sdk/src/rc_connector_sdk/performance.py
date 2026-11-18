"""Connector-local optional timing markers; no policy SDK dependency."""
import os
import threading
import time


class FeedbackTimings:
    """Collect ROS callback arrival times until RC's next state poll."""

    def __init__(self, capacity=1024):
        self._capacity = capacity
        self._lock = threading.Lock()
        self._events = []
        self._dropped = 0
        self._closed = False

    def received(self, arm, kind):
        wall_ns, mono_ns = time.time_ns(), time.monotonic_ns()
        with self._lock:
            if self._closed:
                return
            if len(self._events) == self._capacity:
                self._dropped += 1
                return
            self._events.append({'arm': arm, 'feedback_kind': kind,
                                 'feedback_received_timestamp_ns': wall_ns,
                                 'feedback_received_monotonic_ns': mono_ns})

    def drain_into(self, response, *, closing=False):
        with self._lock:
            if closing:
                self._closed = True
            if self._events or self._dropped:
                response['_feedback_timing'] = {'events': self._events, 'dropped': self._dropped}
                self._events, self._dropped = [], 0


def mark(message, stage, **values):
    data = message.get('_performance') if isinstance(message, dict) else None
    if (not isinstance(data, dict) or data.get('version') != 1
            or not isinstance(data.get('points'), list)
            or type(data.get('dropped_points', 0)) is not int):
        return
    if len(data['points']) >= 128:
        data['dropped_points'] = min(2**31-1, data.get('dropped_points', 0)+1)
        return
    data['points'].append({'stage': stage, 'monotonic_ns': time.monotonic_ns(),
                           'pid': os.getpid(), 'thread': threading.current_thread().name,
                           **{k:v for k,v in values.items() if type(v) in (str,int,bool)}})
