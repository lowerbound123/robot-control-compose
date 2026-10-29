"""RC-owned serialized state broker, independent of the robot/lifecycle loops."""
from collections import OrderedDict
from concurrent.futures import Future
from contextlib import nullcontext
import math
from pathlib import Path
import queue
import threading
import time

from rc_policy_sdk.global_state import (GlobalStateConflict, GlobalStateProxy, MAX_BYTES,
                                        SharedState, decode, encode, validate_key)
from .global_state_file import read_state, save_state


from .global_state_config import settings


class GlobalStateService:
    def __init__(self, path, ipc_directory, *, run_id, max_bytes=1048576, timeout_s=2.0):
        self.path, self.ipc_directory = Path(path), Path(ipc_directory)
        self.run_id, self.max_bytes, self.timeout_s = run_id, max_bytes, timeout_s
        self.memory = None
        self.thread = None
        self.lock = threading.RLock()
        self.queue = queue.Queue(maxsize=256)
        self.accepting = False
        self.sessions = {}
        self.version, self.raw = 0, b'{}'
        self.proxy = GlobalStateProxy(self.snapshot, self.patch)

    @property
    def descriptor(self):
        if self.memory is None:
            raise RuntimeError('global state unavailable')
        return dict(self.memory.descriptor, run_id=self.run_id)

    def start(self, *, store_id=None, cancel_event=None, deadline=None):
        values = read_state(self.path, self.max_bytes, cancel_event=cancel_event, deadline=deadline)
        self.memory = SharedState.create(self.ipc_directory, values, max_bytes=self.max_bytes,
                                         timeout_s=self.timeout_s, store_id=store_id)
        self.version, self.raw = 1, encode(values, self.max_bytes)
        self.accepting = True
        self.thread = threading.Thread(target=self._run, name='rc-global-state', daemon=True)
        self.thread.start()

    def snapshot(self):
        with self.lock:
            if self.memory is None:
                raise RuntimeError('global state unavailable')
            version, raw = self.version, self.raw
        return version, decode(raw)

    def attach_session(self, worker):
        with self.lock:
            if not self.accepting:
                raise RuntimeError('global state closed')
            binding = dict(run_id=self.run_id, store_id=self.descriptor['store_id'],
                           session_id=worker.session_id, generation=(worker.expected_host or {}).get('generation', 0))
            self.sessions[worker] = dict(binding=binding, requests=OrderedDict())
            return dict(self.descriptor, binding=binding)

    def revoke(self, worker):
        with self.lock:
            self.sessions.pop(worker, None)

    def submit(self, values, delete=(), expected_version=None, *, guard=nullcontext, session=None):
        # Detach caller-owned values before enqueueing, including nested objects.
        raw = encode(values, self.max_bytes)
        delete = list(delete)
        for key in delete:
            validate_key(key)
        if len(set(delete)) != len(delete) or set(delete) & values.keys():
            raise ValueError('conflicting patch keys')
        if expected_version is not None and (type(expected_version) is not int or expected_version < 1):
            raise ValueError('invalid expected_version')
        future = Future()
        with self.lock:
            if not self.accepting or (session is not None and session not in self.sessions):
                raise RuntimeError('global state session closed')
            try:
                self.queue.put_nowait((future, raw, delete, expected_version, guard, session, time.monotonic() + self.timeout_s))
            except queue.Full:
                raise RuntimeError('global state queue full') from None
        return future

    def patch(self, values, delete, expected_version):
        return self.submit(values, delete, expected_version).result(timeout=self.timeout_s)

    def receive(self, worker, message, reply):
        identity = message.get('rpc_id')
        def respond(future):
            try:
                result = dict(type='global_state_ack', rpc_id=identity, version=future.result())
            except Exception as exc:
                result = dict(type='global_state_ack', rpc_id=identity, error=str(exc), error_type=type(exc).__name__)
            try:
                reply(result)
            except (OSError, RuntimeError):
                pass
        try:
            with self.lock:
                record = self.sessions.get(worker)
                if record is None or message.get('binding') != record['binding']:
                    raise RuntimeError('stale global state session')
                if type(identity) is not int or identity <= 0:
                    raise ValueError('invalid global request id')
                requests = record['requests']
                if identity in requests:
                    future = requests[identity]
                elif requests and identity <= next(reversed(requests)):
                    raise ValueError('expired global request id')
                else:
                    future = self.submit(message['values'], message.get('delete', ()), message.get('expected_version'), session=worker)
                    requests[identity] = future
                    while len(requests) > 256 and next(iter(requests.values())).done():
                        requests.popitem(last=False)
            future.add_done_callback(respond)
        except Exception as exc:
            failed = Future()
            failed.set_exception(exc)
            respond(failed)

    def _run(self):
        while True:
            job = self.queue.get()
            if job is None:
                return
            future, raw, deleted, expected, guard, session, deadline = job
            try:
                values = decode(self.raw)
                for key in deleted:
                    del values[key]
                values.update(decode(raw))
                proposed = encode(values, self.max_bytes)
                with self.memory.locked(write=True, deadline=deadline):
                    with guard(), self.lock:
                        if session is not None and session not in self.sessions:
                            raise RuntimeError('stale global state session')
                        if time.monotonic() >= deadline:
                            raise TimeoutError('global state request expired')
                        if expected is not None and expected != self.version:
                            raise GlobalStateConflict('global state version conflict')
                        version = self.version + 1
                        self.memory.publish(version, proposed)
                        self.version, self.raw = version, proposed
                future.set_result(version)
            except Exception as exc:
                future.set_exception(exc)

    def freeze(self, deadline):
        with self.lock:
            self.accepting = False
        if self.thread is not None and self.thread.is_alive():
            self.queue.put(None, timeout=max(0, deadline - time.monotonic()))
            self.thread.join(max(0, deadline - time.monotonic()))
            if self.thread.is_alive():
                raise TimeoutError('global state drain deadline exceeded')
        with self.lock:
            self.sessions.clear()

    def finish(self, deadline):
        if self.memory is not None:
            self.freeze(deadline)
            save_state(self.path, self.snapshot()[1], deadline)

    def close(self, deadline, *, reclaim=True):
        self.freeze(deadline)
        if self.memory is not None:
            self.memory.close(unlink=reclaim)
            self.memory = None
