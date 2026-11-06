"""Bounded asynchronous command submission; queries use a separate UDS."""
from collections import deque
import socket
import select
import errno
import threading
import time

from .actions import validate_arm_targets
from .wire import encode_message, receive_message, send_message


class _DeadlineIO:
    """A framed exchange uses one budget, including every partial read/write."""
    def __init__(self, connection, deadline, cancel_event=None):
        self.connection, self.deadline, self.cancel_event = connection, deadline, cancel_event

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or self.cancel_event is not None and self.cancel_event.is_set():
            raise TimeoutError('connector request cancelled or deadline exceeded')
        return remaining

    def wait(self, *, write=False):
        while True:
            timeout = min(.02, self.remaining())
            readable, writable, _ = select.select([] if write else [self.connection],
                [self.connection] if write else [], [], timeout)
            self.remaining()
            if readable or writable: return

    def recv(self, length):
        while True:
            self.wait()
            try:
                result = self.connection.recv(length, socket.MSG_DONTWAIT)
                self.remaining()
                return result
            except (BlockingIOError, InterruptedError): continue

    def sendall(self, data):
        view = memoryview(data)
        while view:
            self.wait(write=True)
            try: size = self.connection.send(view, socket.MSG_DONTWAIT)
            except (BlockingIOError, InterruptedError): continue
            if not size: raise OSError('connector socket closed')
            view = view[size:]
        self.remaining()


class ConnectorClient:
    def __init__(self, endpoint, *, timeout=5., max_pending=32, wake=None, deadline=None, cancel_event=None):
        self.endpoint, self.timeout, self.max_pending = str(endpoint), timeout, max_pending
        self.wake = wake or (lambda: None)
        self.closed = False
        self.failure = None
        self.serial = 0
        self._lock = threading.RLock()
        self._query_lock = threading.Lock()
        self._pending, self._results, self._outbox = {}, deque(), deque()
        self._command = self._query = None
        try:
            self._query = self._connect('.query', deadline=deadline, cancel_event=cancel_event)
            self.info = self._request('describe', request_deadline=deadline, cancel_event=cancel_event)
            if self.info.get('protocol_version') != 1:
                raise RuntimeError('connector protocol mismatch')
            self._command = self._connect('.command', deadline=deadline, cancel_event=cancel_event)
            self._command.settimeout(None)
            self._reader = threading.Thread(target=self._read, name='connector-receipts', daemon=True)
            self._reader.start()
        except BaseException:
            self.disconnect(timeout=max(0., deadline - time.monotonic()) if deadline is not None else 1.)
            raise

    def _connect(self, suffix, *, deadline=None, cancel_event=None):
        connection = socket.socket(socket.AF_UNIX)
        try:
            budget = _DeadlineIO(connection, min(deadline if deadline is not None else float('inf'), time.monotonic() + self.timeout), cancel_event)
            connection.setblocking(False)
            while True:
                budget.remaining()
                error = connection.connect_ex(self.endpoint + suffix)
                if error in (0, errno.EISCONN): break
                if error in (errno.EAGAIN, errno.EWOULDBLOCK):
                    if cancel_event is not None: cancel_event.wait(min(.02, budget.remaining()))
                    else: time.sleep(min(.02, budget.remaining()))
                    continue
                if error in (errno.EINPROGRESS, errno.EALREADY):
                    budget.wait(write=True)
                    error = connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                    if not error: break
                raise OSError(error, 'connector socket connection failed')
            budget.remaining()
            connection.settimeout(self.timeout)
            return connection
        except BaseException:
            connection.close()
            raise

    def _request(self, method, *, request_deadline=None, cancel_event=None, **params):
        budget = _DeadlineIO(self._query,
            min(request_deadline if request_deadline is not None else float('inf'), time.monotonic() + self.timeout), cancel_event)
        while not self._query_lock.acquire(timeout=min(.02, budget.remaining())):
            pass
        try:
            if self.closed: raise RuntimeError('connector is closed')
            try:
                send_message(budget, {'method': method, 'params': params})
                response = receive_message(budget)
                budget.remaining()
            except (OSError, EOFError, ValueError):
                # A partial or late frame may never become a later query's ack.
                self._query.close()
                raise
            if response.get('error'): raise RuntimeError(response['error'])
            return response['result']
        finally:
            self._query_lock.release()

    def describe(self, *, deadline=None, cancel_event=None):
        return self._request('describe', request_deadline=deadline, cancel_event=cancel_event)

    def get_state(self, since=None, *, deadline=None, cancel_event=None):
        return self._request('get_state', request_deadline=deadline, cancel_event=cancel_event, since=since)

    def get_images(self, cameras, *, deadline=None, cancel_event=None):
        return self._request('get_images', request_deadline=deadline, cancel_event=cancel_event, cameras=list(cameras))

    def release_images(self, lease_ids, *, deadline=None, cancel_event=None):
        return self._request('release_images', request_deadline=deadline, cancel_event=cancel_event, lease_ids=list(lease_ids))

    def submit(self, targets):
        targets = validate_arm_targets(targets)
        with self._lock:
            if self.closed or self.failure: raise RuntimeError(self.failure or 'connector is closed')
            if len(self._pending) >= self.max_pending: raise RuntimeError('connector command queue is full')
            self.serial += 1
            identity = self.serial
            self._pending[identity] = targets
            self._outbox.append(memoryview(encode_message({'id': identity, 'method': 'send_targets', 'params': targets})))
            self._flush()
            return identity

    def _flush(self):
        # Called by the control thread, never by the receipt reader. Partial
        # writes remain FIFO; a take/pause never removes an admitted frame.
        while self._outbox and not self.failure:
            try:
                size = self._command.send(self._outbox[0], socket.MSG_DONTWAIT)
                if not size: raise OSError('connector socket closed')
            except (BlockingIOError, InterruptedError):
                break
            except OSError as exc:
                self._fail(str(exc))
                break
            view = self._outbox.popleft()
            if size < len(view): self._outbox.appendleft(view[size:])

    def _fail(self, error):
        with self._lock:
            if self.failure: return
            self.failure = error
            received = {r['id'] for r in self._results}
            for identity, targets in self._pending.items():
                if identity not in received:
                    self._results.append({'id': identity, 'success': False, 'error': error,
                        'arms': {arm: 'unknown' for arm in targets}, 'events': [], 'feedback_watermarks': {}})
            self._outbox.clear()
        self.wake()

    def _read(self):
        try:
            while not self.closed:
                response = receive_message(self._command)
                with self._lock:
                    identity = response.get('id')
                    if type(identity) is not int or not 0 < identity <= self.serial:
                        raise ValueError('unexpected connector command receipt')
                    # IDs are monotonically issued without gaps. An issued ID
                    # absent from pending was already delivered to the caller.
                    if identity not in self._pending: continue
                    if any(r['id'] == response['id'] for r in self._results): continue
                    self._results.append(response)
                self.wake()
        except (OSError, EOFError, ValueError) as exc:
            if not self.closed: self._fail(f'connector disconnected: {exc}')

    def poll_results(self):
        with self._lock:
            self._flush()
            result = list(self._results)
            self._results.clear()
            for response in result: self._pending.pop(response['id'], None)
            return result

    def close(self, *, timeout=5., on_results=None):
        if self.closed: return []
        drained = []
        deadline = time.monotonic() + timeout
        while self._pending:
            batch = self.poll_results()
            drained.extend(batch)
            if on_results is not None: on_results(batch)
            if time.monotonic() >= deadline: raise TimeoutError('connector command drain timed out')
            time.sleep(.001)
        self._query.settimeout(max(.001, deadline - time.monotonic()))
        try:
            result = self._request('close', request_deadline=deadline, deadline=deadline)
            if not result.get('clean'): raise RuntimeError('connector has unreleased image readers')
        finally:
            self.disconnect(timeout=max(0., deadline - time.monotonic()))
        return drained

    def disconnect(self, *, timeout=1.):
        self.closed = True
        for connection in (self._command, self._query):
            if connection is not None:
                try: connection.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                connection.close()
        reader = getattr(self, '_reader', None)
        if reader is not None and reader is not threading.current_thread(): reader.join(max(0., timeout))
