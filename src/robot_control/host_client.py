"""Multiplexed UDS connection. A queued operation is never replayed."""
import queue
import socket
import threading
import time
import uuid
import weakref

from rc_policy_sdk.outbox import PriorityOutbox
from rc_policy_sdk.wire import encode_message, receive_message, send_message


class HostClient:
    _clients = weakref.WeakValueDictionary()
    _registry_lock = threading.Lock()

    @classmethod
    def get(cls, socket_path, host_id, *, expected=None, connect_timeout=3):
        key = (str(socket_path), host_id, tuple(sorted((expected or {}).items())))
        with cls._registry_lock:
            client = cls._clients.get(key)
            if client is None or client.closed:
                client = cls(str(socket_path), host_id, expected=expected, connect_timeout=connect_timeout)
                cls._clients[key] = client
            return client

    def __init__(self, path, host_id, *, expected=None, connect_timeout=3):
        self.path, self.host_id = path, host_id
        self.expected = expected or {}
        self._heartbeat_stop = threading.Event()
        self.owner_id = uuid.uuid4().hex
        self.members = {}
        self.condition = threading.Condition(threading.RLock())
        self.outbox = PriorityOutbox(32)
        self.results = {}
        self.serial = 0
        self.closed = False
        self.failure = None
        self.connection = socket.socket(socket.AF_UNIX)
        try:
            self.connection.settimeout(connect_timeout)
            self.connection.connect(path)
            info = self._hello(self.connection)
            self.instance_id = info['host_instance_id']
            self.connection.settimeout(None)
        except BaseException:
            self.connection.close(); raise
        self.threads = [threading.Thread(target=self._read, name='host-reader', daemon=True),
                        threading.Thread(target=self._write, name='host-writer', daemon=True),
                        threading.Thread(target=self._heartbeat, name='host-heartbeat', daemon=True)]
        for thread in self.threads: thread.start()

    def _hello(self, connection, *, require_healthy=True):
        send_message(connection, {'type':'hello','rpc_id':0,'owner_id':self.owner_id,
            'protocol_version':1,'image_protocol_version':1})
        response = receive_message(connection)
        if response.get('error'): raise RuntimeError(response['error'])
        if response.get('type') != 'rpc_result' or not isinstance(response.get('result'), dict):
            raise RuntimeError('incompatible legacy host protocol; upgrade rc-policy-sdk')
        info = response['result']
        if info.get('host_id') != self.host_id or info.get('protocol_version') != 1 or info.get('image_protocol_version') != 1:
            raise RuntimeError('policy host identity/protocol mismatch')
        if (require_healthy and not info.get('healthy')) or any(info.get(key) != value for key,value in self.expected.items()):
            raise RuntimeError('policy host unavailable or stale generation')
        return info

    def _heartbeat(self):
        while not self._heartbeat_stop.wait(1):
            try:
                if not self.request('ping', timeout=3).get('healthy') and not self._heartbeat_stop.is_set():
                    raise RuntimeError('policy host unavailable')
            except Exception as exc:
                if not self._heartbeat_stop.is_set():
                    self._fail(str(exc))
                return

    def register(self, member):
        with self.condition: self.members[member.session_id] = member

    def enqueue(self, message):
        framed = encode_message(message)
        with self.condition:
            if self.closed or self.failure: raise RuntimeError(self.failure or 'host disconnected')
            try: self.outbox.put_nowait(framed)
            except queue.Full as exc: raise RuntimeError('host request queue is full') from exc

    def enqueue_monitor(self, message):
        framed = encode_message(message)
        with self.condition:
            if self.closed or self.failure:
                raise RuntimeError(self.failure or 'host disconnected')
            if message['session_id'] not in self.members:
                raise ValueError('unknown monitor session')
            return self.outbox.put_monitor(message['session_id'], framed)

    def request(self, kind, *, timeout, **payload):
        with self.condition:
            self.serial += 1
            identity = self.serial
            self.enqueue({'type':kind, 'rpc_id':identity, **payload})
            deadline = time.monotonic() + timeout
            while identity not in self.results:
                if self.failure or self.closed: raise RuntimeError(self.failure or 'host disconnected')
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise TimeoutError(f'host {kind} timeout')
                self.condition.wait(min(.05, remaining))
            result = self.results.pop(identity)
        if result.get('error'): raise RuntimeError(result['error'])
        return result.get('result')

    def preflight(self, policy_id, timeout=10):
        return self.request('preflight', timeout=timeout, policy_id=policy_id)

    def _fail(self, message):
        with self.condition:
            self.failure = message
            members = list(self.members.values())
            self.condition.notify_all()
        for member in members: member._record_failure(message)

    def _write(self):
        try:
            while True:
                framed = self.outbox.get()
                if framed is None: return
                self.connection.sendall(framed)
        except OSError as exc: self._fail(f'host write failed: {exc}')

    def _read(self):
        try:
            while True:
                message = receive_message(self.connection)
                kind = message.get('type')
                if kind == 'rpc_result':
                    with self.condition:
                        self.results[message['rpc_id']] = message
                        self.condition.notify_all()
                    continue
                if message.get('host_instance_id') != self.instance_id:
                    raise ValueError('stale host instance')
                with self.condition: members = list(self.members.values())
                if kind == 'policy_event':
                    member = self.members.get(message['session_id'])
                    if member: member._receive(message)
                elif kind == 'runtime_stopped':
                    for member in members: member._confirm_closed(message)
                elif kind == 'host_fault':
                    for member in members:
                        if member.runtime_id == message['runtime_id']: member._record_failure(message['error'])
                elif kind == 'log':
                    for member in members:
                        if member.runtime_id == message['runtime_id']:
                            if hasattr(member, '_log_packet'): member._log_packet(message)
                            else: member._log(message['message'])
                else: raise ValueError(f'unexpected host message: {kind}')
        except (OSError, EOFError, ValueError) as exc:
            if not self.closed: self._fail(f'host disconnected: {exc}')

    def stop_group(self, runtime_id, sessions, timeout, *, deadline=None):
        # Closing is expected during release; keep the RPC reader alive for ACKs.
        self._heartbeat_stop.set()
        deadline = deadline if deadline is not None else time.monotonic() + timeout + 6
        def left():
            value = deadline - time.monotonic()
            if value <= 0: raise TimeoutError('host cleanup deadline exceeded')
            return value
        payload = dict(runtime_id=runtime_id, sessions=sessions, release_timeout=min(timeout, max(0, left()-5)))
        try:
            proof = self.request('stop', timeout=left(), **payload)
        except (OSError, RuntimeError, TimeoutError):
            # Discard queued load/dispatch before opening a cleanup-only connection.
            self.disconnect()
            with socket.socket(socket.AF_UNIX) as rescue:
                rescue.settimeout(left()); rescue.connect(self.path)
                info = self._hello(rescue, require_healthy=False)
                if info['host_instance_id'] != self.instance_id:
                    raise RuntimeError('host restarted; old readers cannot be confirmed')
                send_message(rescue, {'type':'stop', 'rpc_id':1, **payload})
                result = receive_message(rescue)
                if result.get('error'): raise RuntimeError(result['error'])
                proof = result['result']
        if not proof.get('cleanup_confirmed') or proof.get('host_instance_id') != self.instance_id:
            raise RuntimeError('missing runtime exit proof')
        for member in list(self.members.values()): member._confirm_closed(proof)
        self.disconnect()
        return proof['forced']

    def disconnect(self):
        self._heartbeat_stop.set()
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        try: self.connection.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        while True:
            try: self.outbox.get_nowait()
            except queue.Empty: break
        try: self.outbox.put_nowait(None)
        except queue.Full: pass
        self.connection.close()
        for thread in self.threads:
            if thread is not threading.current_thread(): thread.join(1)
