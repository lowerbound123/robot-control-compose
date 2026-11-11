"""Control first, retained monitors next, bounded best-effort logs last.

Monitor keys are internal, registered policy/session identities, never user metric
keys. Their capacity is separate from the bounded control queue. A duplicate is
refused before ownership transfers, rather than replacing an accepted message.
Logs have separate count/byte limits and one aggregated loss notice. A pending
notice closes log admission until it is consumed, preserving FIFO gap ordering.
"""
from collections import OrderedDict, deque
import json
import queue
import threading
import time

from .wire import encode_message

MAX_LOG_GAP_COUNT = (1 << 63) - 1

# A gap never closes over a discarded table/result, arbitrary extras, or an
# unbounded user-provided "identity". These are transport identities only.
LOG_IDENTITY = ('type', 'host_instance_id', 'host_id', 'policy_instance_id',
                'source_policy_id', 'deployment_id', 'run_id', 'generation',
                'runtime_id', 'session_id', 'context_id', 'config_version', 'request_id')


def _log_identity(message):
    return {key: value for key in LOG_IDENTITY if key in message
            for value in (message[key],)
            if (type(value) is str and len(value) <= 256)
            or (type(value) is int and value.bit_length() <= 63)
            or value is None}


def log_gap_factory(message, *, stage, structured=True):
    """Return a frame factory retaining only bounded identity/stage scalars."""
    outer = _log_identity(message)
    nested = isinstance(message.get('event'), dict)
    identity = _log_identity(message['event']) if nested else outer
    stage = stage if stage in ('worker', 'runtime', 'host') else 'host'

    def gap(count, saturated):
        details = {'event': 'sdk.log_gap', 'count': count}
        if saturated:
            details['count_saturated'] = True
        event = {**identity, 'type': 'log', 'message': json.dumps(details, separators=(',', ':'))}
        if structured:
            event.update(level='warning', log_event='sdk.log_gap', stage=stage, count=count)
            if saturated:
                event['count_saturated'] = True
        return encode_message({**outer, 'event': event} if nested else event)
    return gap


def log_loss_weight(message):
    """A dropped upstream gap represents its original lost record count."""
    event = message.get('event', message)
    count = event.get('count') if isinstance(event, dict) else None
    if (isinstance(event, dict) and event.get('type') == 'log' and event.get('log_event') == 'sdk.log_gap'
            and type(count) is int and 0 < count <= MAX_LOG_GAP_COUNT):
        return count, event.get('count_saturated') is True
    return 1, False


class PriorityOutbox:
    def __init__(self, maxsize=0, *, log_maxsize=256, log_maxbytes=1024 * 1024):
        if (type(log_maxsize) is not int or log_maxsize <= 0
                or type(log_maxbytes) is not int or log_maxbytes <= 0):
            raise ValueError("log count and byte limits must be positive integers")
        self.maxsize = maxsize
        self.log_maxsize = log_maxsize
        self.log_maxbytes = log_maxbytes
        self._normal = deque()
        self._monitor = OrderedDict()
        self._logs = deque()
        self._log_bytes = 0
        self._log_dropped = 0
        self._log_saturated = False
        self._log_gap_factory = None
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

    def put_log(self, item, *, size, gap_factory, loss_count=1, loss_saturated=False):
        """Try to retain an opaque log item; overflow never raises queue.Full.

        ``size`` is its encoded byte size. The first dropped item's factory
        creates one opaque gap item from ``(count, saturated)`` when drained.
        It must retain only bounded metadata, not the discarded log payload.
        A saturated count is a lower bound; the counter itself cannot grow.
        ``loss_count`` carries an upstream gap through another dropping stage;
        the stage on the resulting notice names its latest aggregation point.
        """
        if (type(size) is not int or size < 0 or not callable(gap_factory)
                or type(loss_count) is not int or not 0 < loss_count <= MAX_LOG_GAP_COUNT
                or type(loss_saturated) is not bool):
            raise ValueError("log size must be nonnegative and gap_factory callable")
        with self._condition:
            if (self._log_dropped or len(self._logs) >= self.log_maxsize
                    or self._log_bytes + size > self.log_maxbytes):
                if not self._log_dropped:
                    self._log_gap_factory = gap_factory
                total = self._log_dropped + loss_count
                self._log_dropped = min(total, MAX_LOG_GAP_COUNT)
                self._log_saturated |= loss_saturated or total > MAX_LOG_GAP_COUNT
                self._condition.notify_all()
                return False
            self._logs.append((item, size))
            self._log_bytes += size
            self._condition.notify_all()
            return True

    def get(self, block=True, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        gap_factory = None
        with self._condition:
            while not self._normal and not self._monitor and not self._logs and not self._log_dropped:
                if not block:
                    raise queue.Empty
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise queue.Empty
                self._condition.wait(remaining)
            if self._normal:
                item = self._normal.popleft()
            elif self._monitor:
                item = self._monitor.popitem(last=False)[1]
            elif self._logs:
                item, size = self._logs.popleft()
                self._log_bytes -= size
            else:
                gap_factory = self._log_gap_factory
                count, saturated = self._log_dropped, self._log_saturated
                self._log_gap_factory = None
                self._log_dropped = 0
                self._log_saturated = False
            self._condition.notify_all()
        return gap_factory(count, saturated) if gap_factory is not None else item

    def get_nowait(self):
        return self.get(block=False)

    def qsize(self):
        with self._condition:
            return len(self._normal) + len(self._monitor) + len(self._logs) + bool(self._log_dropped)


class PrioritySender:
    """Control sends, retained monitors and nonblocking bounded log admission."""
    def __init__(self, connection, maxsize=64):
        self.connection = connection
        self.outbox = PriorityOutbox(maxsize)
        self.lock = threading.Lock()
        self.failure = None
        self.failed = threading.Event()
        self.thread = threading.Thread(target=self._write, name="policy-output", daemon=True)
        self.thread.start()

    def send(self, message, *, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
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
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('policy output queue timeout')
            self.failed.wait(.005)
        if not item[1].wait(None if deadline is None else max(0, deadline - time.monotonic())):
            raise TimeoutError('policy output acknowledgement timeout; delivery uncertain')
        if item[2]:
            raise OSError(str(item[2][0]))

    def send_monitor(self, message, key="monitor"):
        item = (encode_message(message), None, [])
        with self.lock:
            if self.failure:
                raise OSError(str(self.failure))
            return self.outbox.put_monitor(key, item)

    def send_log(self, message, *, stage='worker'):
        packet = encode_message(message)
        gap = log_gap_factory(message, stage=stage)
        count, saturated = log_loss_weight(message)
        with self.lock:
            if self.failure:
                return False
            return self.outbox.put_log((packet, None, []), size=len(packet),
                gap_factory=lambda count, saturated: (gap(count, saturated), None, []),
                loss_count=count, loss_saturated=saturated)

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
