"""Connector-local optional timing markers; no policy SDK dependency."""
import os
import threading
import time


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
