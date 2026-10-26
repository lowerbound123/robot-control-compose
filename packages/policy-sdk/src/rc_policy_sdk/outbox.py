"""Control messages first; one retained low-priority message per live session.

Monitor keys are internal, registered policy/session identities, never user metric
keys. Their capacity is separate from the bounded control queue. A duplicate is
refused before ownership transfers, rather than replacing an accepted message.
"""
from collections import OrderedDict, deque
import queue
import threading
import time

from .wire import encode_message


class PriorityOutbox:
    def __init__(self, maxsize=0):
        self.maxsize = maxsize
        self._normal = deque()
        self._monitor = OrderedDict()
        self._condition = threading.Condition()

    def put(self, item, block=True, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while self.maxsize > 0 and len(self._normal) >= self.maxsize:
                if not block:
                    raise queue.Full
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise queue.Full
                self._condition.wait(remaining)
            self._normal.append(item)
            self._condition.notify_all()

    def put_nowait(self, item):
        self.put(item, block=False)

    def put_monitor(self, key, item):
        with self._condition:
            if key in self._monitor:
                return False
            self._monitor[key] = item
            self._condition.notify_all()
            return True

    def get(self, block=True, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while not self._normal and not self._monitor:
                if not block:
                    raise queue.Empty
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)
            item = self._normal.popleft() if self._normal else self._monitor.popitem(last=False)[1]
            self._condition.notify_all()
            return item

    def get_nowait(self):
        return self.get(block=False)

    def qsize(self):
        with self._condition:
            return len(self._normal) + len(self._monitor)

    def empty(self):
        return self.qsize() == 0


class PrioritySender:
    """Synchronous control sends and nonblocking, retained monitor completions."""
    def __init__(self, connection, maxsize=64):
        self.connection = connection
        self.outbox = PriorityOutbox(maxsize)
        self.lock = threading.Lock()
        self.failure = None
        self.failed = threading.Event()
        self.thread = threading.Thread(target=self._write, name="policy-output", daemon=True)
        self.thread.start()

    def send(self, message):
        item = (encode_message(message), threading.Event(), [])
        while True:
            with self.lock:
                if self.failure:
                    raise OSError(str(self.failure))
                try:
                    self.outbox.put_nowait(item)
                    break
                except queue.Full:
                    pass
            # Control sends previously waited on the socket send lock. Keep
            # that backpressure without holding the failure/close lock.
            self.failed.wait(.005)
        item[1].wait()
        if item[2]:
            raise OSError(str(item[2][0]))

    def send_monitor(self, message, key="monitor"):
        item = (encode_message(message), None, [])
        with self.lock:
            if self.failure:
                raise OSError(str(self.failure))
            return self.outbox.put_monitor(key, item)

    def _fail(self, exc):
        with self.lock:
            self.failure = exc
            self.failed.set()
            while True:
                try: item = self.outbox.get_nowait()
                except queue.Empty: break
                if item is not None and item[1] is not None:
                    item[2].append(exc)
                    item[1].set()

    def _write(self):
        while True:
            item = self.outbox.get()
            if item is None:
                return
            try:
                self.connection.sendall(item[0])
            except OSError as exc:
                item[2].append(exc)
                self._fail(exc)
                return
            finally:
                if item[1] is not None:
                    item[1].set()

    def close(self):
        self._fail(OSError("policy output closed"))
        self.outbox.put_nowait(None)
        if self.thread is not threading.current_thread():
            self.thread.join(1)
