"""Optional read-only scalar snapshots, isolated from inference and commands."""
import inspect
import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor

from .command_runtime import _error_text

MAX_MONITOR_ITEMS = 128
MAX_MONITOR_BYTES = 64 * 1024
MONITOR_IDENTITY = ("monitor_id", "monitor_epoch", "config_version")


def normalize_snapshot(value):
    if type(value) is not dict:
        raise TypeError("monitor() must return a dict")
    if len(value) > MAX_MONITOR_ITEMS:
        raise ValueError("monitor snapshot exceeds 128 items")
    result = {}
    for key, item in value.items():
        if type(key) is not str:
            raise TypeError("monitor keys must be strings")
        if type(item) not in (float, int, str):
            raise TypeError("monitor values must be built-in float, int or str")
        if type(item) is float and not math.isfinite(item):
            raise ValueError("monitor floats must be finite")
        result[key] = item
    raw = json.dumps(result, allow_nan=False, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_MONITOR_BYTES:
        raise ValueError("monitor snapshot exceeds 64 KiB")
    return result


def monitor_callback(module):
    callback = getattr(module, "monitor", None)
    if not callable(callback):
        return None
    if inspect.iscoroutinefunction(callback) or inspect.isgeneratorfunction(callback) or inspect.isasyncgenfunction(callback):
        return None
    try:
        inspect.signature(callback).bind(None)
    except (TypeError, ValueError):
        return None
    return callback


class MonitorExecutor:
    def __init__(self, channel, context, callback):
        self.channel, self.context, self.callback = channel, context, callback
        self.condition = threading.Condition(threading.RLock())
        self.accepting = True
        self.busy = False
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="policy-monitor")

    def submit(self, message, version):
        identity = {key: message.get(key) for key in MONITOR_IDENTITY}
        with self.condition:
            error = None
            if any(type(identity[key]) is not int or identity[key] < 0 for key in MONITOR_IDENTITY):
                error = "invalid monitor identity"
            elif identity["config_version"] != version:
                error = "monitor config version differs from worker"
            elif self.callback is None:
                error = "policy does not support monitor(context)"
            elif not self.accepting or self.busy:
                error = "monitor busy: callback or lifecycle operation"
            if error:
                self.channel.send_monitor({"type": "monitor_error", **identity, "error": error})
                return
            self.busy = True
            self.pool.submit(self._invoke, identity)

    def _invoke(self, identity):
        try:
            response = {"type": "monitor_result", **identity,
                        "result": normalize_snapshot(self.callback(self.context))}
        except BaseException as exc:
            response = {"type": "monitor_error", **identity, "error": _error_text(exc)}
        with self.condition:
            try:
                # Queue a detached, bounded result; never wait on socket I/O
                # while retaining the user's context lifecycle barrier.
                self.channel.send_monitor(response)
            except OSError:
                pass
            finally:
                self.busy = False
                self.condition.notify_all()

    def pause(self):
        with self.condition:
            self.accepting = False

    def barrier(self):
        with self.condition:
            self.accepting = False
            while self.busy:
                self.condition.wait()

    def resume(self):
        with self.condition:
            self.accepting = True

    def close(self):
        self.barrier()
        self.pool.shutdown(wait=True)
