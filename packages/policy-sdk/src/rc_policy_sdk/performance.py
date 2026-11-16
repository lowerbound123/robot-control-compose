"""Optional, bounded timing metadata. No logging, I/O, or model dependencies."""
from contextlib import contextmanager
import math
import os
import threading
import time

KEY = '_performance'
MAX_POINTS = 128


def scalars(values):
    return {str(k)[:80]: (v[:160] if isinstance(v, str) else v)
            for k, v in values.items()
            if v is None or isinstance(v, (str, bool, int))
            or isinstance(v, float) and math.isfinite(v)}


class Trace:
    def __init__(self, data):
        self.data = data

    @classmethod
    def start(cls, identity):
        return cls({'version': 1, 'trace_id': str(identity)[:160], 'points': [], 'dropped_points': 0})

    def mark(self, stage, **values):
        points = self.data['points']
        if len(points) >= MAX_POINTS:
            self.data['dropped_points'] = min(2**31-1, self.data.get('dropped_points', 0) + 1)
            return
        points.append({**scalars(values), 'stage': str(stage)[:80],
                       'monotonic_ns': time.monotonic_ns(), 'pid': os.getpid(),
                       'thread': threading.current_thread().name[:80]})

    @contextmanager
    def span(self, stage, **values):
        started = time.monotonic_ns()
        try:
            yield
        finally:
            self.mark(stage, started_ns=started, duration_ns=time.monotonic_ns()-started, **values)

    def snapshot(self):
        return {'version': 1, 'trace_id': self.data['trace_id'],
                'points': [scalars(p) for p in self.data['points'][:MAX_POINTS]],
                'dropped_points': self.data.get('dropped_points', 0)}

    def attach(self, message):
        message[KEY] = self.data
        return message


def trace(message, stage=None, **values):
    data = message.get(KEY) if isinstance(message, dict) else None
    if (not isinstance(data, dict) or data.get('version') != 1
            or not isinstance(data.get('trace_id'), str) or len(data['trace_id']) > 160
            or not isinstance(data.get('points'), list) or len(data['points']) > MAX_POINTS
            or any(not isinstance(p, dict) for p in data['points'])
            or type(data.get('dropped_points', 0)) is not int):
        return None
    result = Trace(data)
    if stage is not None:
        result.mark(stage, **values)
    return result
