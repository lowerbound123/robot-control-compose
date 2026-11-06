"""Bounded asynchronous command submission; queries use a separate UDS."""
from collections import deque
import socket
import threading
import time

from .actions import validate_arm_targets
from .wire import encode_message, receive_message, send_message


class ConnectorClient:
    def __init__(self, endpoint, *, timeout=5., max_pending=32, wake=None):
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
            self._query = self._connect('.query')
            self.info = self._request('describe')
            if self.info.get('protocol_version') != 1:
                raise RuntimeError('connector protocol mismatch')
            self._command = self._connect('.command')
            self._command.settimeout(None)
            self._reader = threading.Thread(target=self._read, name='connector-receipts', daemon=True)
            self._reader.start()
        except BaseException:
            self.disconnect()
            raise

    def _connect(self, suffix):
        connection = socket.socket(socket.AF_UNIX)
        try:
            connection.settimeout(self.timeout)
            connection.connect(self.endpoint + suffix)
            return connection
        except BaseException:
            connection.close()
            raise

    def _request(self, method, **params):
        with self._query_lock:
            if self.closed: raise RuntimeError('connector is closed')
            try:
                send_message(self._query, {'method': method, 'params': params})
                response = receive_message(self._query)
            except (OSError, EOFError, ValueError):
                # A timed-out stream cannot be reused: its late reply could be
                # mistaken for the next query (particularly an image release).
                self._query.close()
                raise
            if response.get('error'): raise RuntimeError(response['error'])
            return response['result']

    def describe(self): return self._request('describe')
    def get_state(self, since=None): return self._request('get_state', since=since)
    def get_images(self, cameras): return self._request('get_images', cameras=list(cameras))
    def release_images(self, lease_ids): return self._request('release_images', lease_ids=list(lease_ids))

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
            result = self._request('close', deadline=deadline)
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
