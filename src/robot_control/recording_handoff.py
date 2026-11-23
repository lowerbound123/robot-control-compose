"""Bounded producer handoff. CPython SimpleQueue.put never waits for a reader."""
from collections import deque
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass, field
import os
from queue import SimpleQueue
import sys
import threading
import time


@dataclass
class _Loss:
    session: str | None
    total: int = 0
    reported: int = 0
    ticket: deque = field(default_factory=lambda: deque([None]))


class ControlRecordingHandoff:
    def __init__(self, report_loss):
        self._queue = SimpleQueue()
        self._report_loss = report_loss
        # popleft/append transfer fixed credits without a recorder mutex.
        # Image references get their own small bound, independent of DEBUG traffic.
        self._credits = {lane: deque([None] * limit) for lane, limit in
                         (('data', 256), ('images', 2), ('priority', 256), ('published', 256),
                          ('debug', 256), ('loop', 256), ('command', 32))}
        self._local = threading.local()
        self.producer = None
        self.closed = False
        self.sealed_session = None
        self._offering = set()
        self.thread = threading.Thread(target=self._run, name='record-control-ingress', daemon=True)

    def pending(self):
        return self._queue.qsize()

    def on_control(self):
        return threading.current_thread() is self.producer

    def offer(self, lane, session, function, *args, run_log=False, oversized=False, **kwargs):
        ticket = object()
        self._offering.add(ticket)
        try:
            if self.closed:
                return False
            if session is not None and session == self.sealed_session:
                # Logs remain eligible for the RC run after the session fence.
                if not run_log:
                    return False
                session = kwargs['binding'] = None
            if oversized:
                return self._reject(lane, session)
            return self._offer(lane, session, function, args, kwargs)
        finally:
            self._offering.discard(ticket)

    def _offer(self, lane, session, function, args, kwargs):
        credits = self._credits[lane]
        try:
            credits.popleft()
        except IndexError:
            return self._reject(lane, session)
        self._queue.put((credits, function, args, kwargs, session))
        return True

    def _reject(self, lane, session):
        # Each producer owns its cumulative counts. A single pending notice
        # per lane/session accounts for bursts while the recorder is stalled.
        if not hasattr(self._local, 'losses'):
            self._local.losses = {}
        loss = self._local.losses.get(lane)
        if loss is None or loss.session != session:
            loss = _Loss(session)
            self._local.losses[lane] = loss
        loss.total += 1
        try:
            loss.ticket.popleft()
        except IndexError:
            return False
        self._queue.put((None, self._flush_loss, (lane, loss), {}, session))
        return False

    def _flush_loss(self, lane, loss):
        loss.ticket.append(None)
        total = loss.total
        delta = total - loss.reported
        loss.reported = total
        if delta:
            self._report_loss(loss.session, lane, delta)

    def fence(self, transition, deadline, *, seal=None):
        """Lifecycle thread orders the session change after all prior offers."""
        if self.producer is None or self.closed:
            transition()
            return
        if seal is not None:
            self.sealed_session = seal
            # A producer that validated this session before the fence must
            # finish its put before the marker. Later offers cannot join it.
            pending = self._offering.copy()
            while not self._offering.isdisjoint(pending):
                if time.monotonic() >= deadline:
                    raise TimeoutError('control recording offer exceeded stop deadline')
                time.sleep(.001)
        finished = Future()

        def apply():
            if finished.set_running_or_notify_cancel():
                try:
                    finished.set_result(transition())
                except Exception as exc:
                    finished.set_exception(exc)
        self._queue.put((None, apply, (), {}, None))
        try:
            finished.result(max(0., deadline - time.monotonic()))
        except FutureTimeout:
            finished.cancel()
            raise TimeoutError('control recording handoff did not drain before deadline')

    def _run(self):
        if sys.platform.startswith('linux'):
            try:
                os.nice(5)  # Linux niceness applies to the calling thread.
            except OSError:
                pass
        while True:
            item = self._queue.get()
            if item is None:
                return
            credits, function, args, kwargs, session = item
            try:
                function(*args, **kwargs)
            except Exception:
                self._report_loss(session, 'snapshot_failed', 1)
            finally:
                if credits is not None:
                    credits.append(None)
                del item, args, kwargs, function

    def close(self, deadline):
        self.closed = True
        # Producers never wait. Only shutdown waits for an offer already
        # between its closed check and the atomic SimpleQueue.put.
        while self._offering and time.monotonic() < deadline:
            time.sleep(.001)
        self._queue.put(None)
        self.thread.join(max(0., deadline - time.monotonic()))
        return not self._offering and not self.thread.is_alive()
