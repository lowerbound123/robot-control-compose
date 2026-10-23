"""Concurrent user callbacks, with a gate for context-changing lifecycle work."""
import json
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor, wait

from .commands import validate_arguments
from .wire import ProtocolError, encode_message


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError(f"duplicate JSON key in command result: {key}")
        result[key] = value
    return result


def _error_text(exc):
    try:
        detail = f"{type(exc).__name__}: {exc}"
    except BaseException:
        detail = "policy command failed (exception text unavailable)"
    truncated = len(detail) > 8192
    detail = detail[:8192].encode("utf-8", "backslashreplace").decode("utf-8")
    return detail + (" ... [truncated]" if truncated else "")


class CommandExecutor:
    def __init__(self, channel, context, specs, handlers, workers):
        self.channel, self.context = channel, context
        self.specs, self.handlers = specs, handlers
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="policy-command")
        self.lock = threading.RLock()
        self.accepting = True
        self.futures = {}

    def submit(self, message, version):
        identity = message.get("command_id")
        try:
            name, args = message["name"], message["args"]
            if message.get("config_version") != version:
                raise ValueError("command config version differs from worker")
            if name not in self.specs:
                raise ValueError("unknown configured policy command")
            validate_arguments(args, self.specs[name])
            with self.lock:
                if not self.accepting:
                    raise ValueError("busy: policy lifecycle operation")
                if identity in self.futures or len(self.futures) >= 256:
                    raise ValueError("duplicate command or full command queue")
                future = self.pool.submit(self._invoke, message)
                self.futures[identity] = future
                future.add_done_callback(lambda result: self._done(message, result))
        except Exception as exc:
            self.channel.send({"type": "command_error", "command_id": identity,
                               "config_version": message.get("config_version"), "error": _error_text(exc)})

    def _invoke(self, message):
        with self.lock:
            if not self.accepting:
                raise CancelledError()
            self.channel.send({"type": "command_started", "command_id": message["command_id"],
                               "config_version": message["config_version"], "started_at": time.monotonic()})
        result = self.handlers[message["name"]](self.context, *message["args"])
        response = {"type": "command_result", "command_id": message["command_id"],
                    "config_version": message["config_version"], "result": result,
                    "finished_at": time.monotonic()}
        # Match the receiver's duplicate-key rules and detach mutable user data.
        # Only this validated JSON snapshot reaches the completion callback.
        return json.loads(encode_message(response)[4:], object_pairs_hook=_json_object)

    def _done(self, message, future):
        identity = message["command_id"]
        try:
            response = future.result()
        except BaseException as exc:
            response = {"type": "command_cancelled" if isinstance(exc, CancelledError) else "command_error",
                        "command_id": identity, "config_version": message["config_version"],
                        "finished_at": time.monotonic(),
                        "error": "cancelled by lifecycle operation" if isinstance(exc, CancelledError)
                        else _error_text(exc)}
        with self.lock:
            self.futures.pop(identity, None)
            try:
                self.channel.send(response)
            except (TypeError, ValueError, RecursionError):
                # A final encoding failure must never strand an unlimited call.
                try:
                    self.channel.send({"type": "command_error", "command_id": identity,
                                       "config_version": message["config_version"],
                                       "finished_at": time.monotonic(),
                                       "error": "policy command response could not be encoded"})
                except OSError:
                    pass
            except OSError:
                pass

    def pause(self):
        with self.lock:
            self.accepting = False
            for future in list(self.futures.values()):
                future.cancel()

    def barrier(self):
        self.pause()
        with self.lock:
            futures = list(self.futures.values())
        wait(futures)

    def resume(self):
        with self.lock:
            self.accepting = True

    def close(self):
        self.pause()
        self.pool.shutdown(wait=True, cancel_futures=True)
