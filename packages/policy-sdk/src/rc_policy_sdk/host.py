"""UDS supervisor. User code lives exclusively in its shared runtime child."""
import argparse
import ast
from collections import OrderedDict
import fcntl
import json
import math
import os
from pathlib import Path
import queue
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

from .lifecycle import become_subreaper, descendant_snapshot, process_birth
from .supervisor import _cleanup
from .outbox import PriorityOutbox
from .monitor import MONITOR_IDENTITY
from .wire import encode_message, receive_message, send_message

PROTOCOL_VERSION = 1
IMAGE_PROTOCOL_VERSION = 1


class Peer:
    def __init__(self, connection):
        self.connection = connection
        self.outbox = PriorityOutbox(64)
        self.owner = None
        threading.Thread(target=self._write, daemon=True).start()

    def send(self, message):
        if message.get('type') == 'log':
            packet = encode_message(message)
            identity = {key: value for key, value in message.items() if key != 'message'}
            def gap_factory(count, saturated):
                event = {'event': 'sdk.log_gap', 'count': count}
                if saturated:
                    event['count_saturated'] = True
                return encode_message({**identity, 'message': json.dumps(event, separators=(',', ':'))})
            return self.outbox.put_log(packet, size=len(packet), gap_factory=gap_factory)
        if message.get('event', {}).get('type') in ('monitor_result', 'monitor_error'):
            return self.outbox.put_monitor(message['session_id'], encode_message(message))
        try: self.outbox.put_nowait(encode_message(message))
        except queue.Full:
            self.close()
            raise ConnectionError('host response queue is full')

    def _write(self):
        try:
            while True:
                packet = self.outbox.get()
                if packet is None: return
                self.connection.sendall(packet)
        except OSError: self.close()

    def close(self):
        try: self.connection.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        try: self.outbox.put_nowait(None)
        except queue.Full: pass


class PolicyHost:
    def __init__(self, root, host_id, *, source_policy_id=None):
        self.root = Path(root).resolve()
        from .catalog import instance_id, policy_id
        self.host_id = instance_id(host_id)
        self.source_policy_id = policy_id(host_id if source_policy_id is None else source_policy_id)
        self.deployment_id = os.environ.get('RC_DEPLOYMENT_ID', '')
        self.run_id = os.environ.get('RC_RUN_ID', '')
        self.generation = int(os.environ.get('RC_GENERATION', '0'))
        self.cleanup_failed = False
        self.owner_seen = time.monotonic()
        self.instance_id = uuid.uuid4().hex
        self.condition = threading.Condition(threading.RLock())
        self.stop_lock = threading.Lock()
        self.process = None
        self.runtime = None
        self.runtime_outbox = None
        self.runtime_id = None
        self.owner = None
        self.known_children = {}
        self.sessions = OrderedDict()
        self.stopped = OrderedDict()
        self.revoked_owners = set()
        self.closing = False
        self.shutting_down = False
        threading.Thread(target=self._watch, daemon=True).start()

    def preflight(self, policy_id):
        if policy_id != self.host_id:
            raise ValueError('host accepts only its assigned policy ID')
        if self.cleanup_failed or self.closing or self.shutting_down:
            raise RuntimeError('host unavailable: cleanup/closing')
        directory = (self.root / self.source_policy_id).resolve()
        if directory.parent != self.root:
            raise ValueError('policy directory escapes the configured root')
        source = (directory / 'main.py').read_text(encoding='utf-8')
        tree = ast.parse(source, filename=str(directory / 'main.py'))
        compile(tree, str(directory / 'main.py'), 'exec')
        names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        required = {'initialize', 'policy', 'reset_context', 'release'}
        if required - names: raise ValueError('main.py missing callbacks: ' + ', '.join(sorted(required - names)))
        return {'valid': True, 'python': sys.version.split()[0], 'directory': str(directory)}

    def _send_runtime(self, message):
        # A blocked runtime must never block the supervisor's watchdog or stop
        # RPC. Ownership transfers into this bounded queue, not into sendall.
        if message.get('event', {}).get('type') == 'monitor':
            if not self.runtime_outbox.put_monitor(message['session_id'], encode_message(message)):
                raise RuntimeError('monitor request already queued')
            return
        try: self.runtime_outbox.put_nowait(encode_message(message))
        except queue.Full as exc: raise RuntimeError('runtime request queue is full') from exc

    def _write_runtime(self, connection, outbox, runtime_id):
        try:
            while True:
                framed = outbox.get()
                if framed is None: return
                connection.sendall(framed)
        except OSError as exc:
            with self.condition:
                active = self.runtime_id == runtime_id and not self.closing
            if active: self.stop(runtime_id, timeout=0, reason=f'runtime write failed: {exc}')

    def _start(self, owner):
        parent, child = socket.socketpair()
        runtime_id = uuid.uuid4().hex
        try:
            process = subprocess.Popen([sys.executable, '-m', 'rc_policy_sdk.host_runtime', '--fd', str(child.fileno())],
                pass_fds=(child.fileno(),), start_new_session=True, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        except BaseException:
            parent.close(); raise
        finally: child.close()
        self.known_children = {}
        self.runtime_outbox = PriorityOutbox(32)
        self.process, self.runtime, self.runtime_id, self.owner = process, parent, runtime_id, owner
        threading.Thread(target=self._write_runtime, args=(parent, self.runtime_outbox, runtime_id), daemon=True).start()
        threading.Thread(target=self._read_runtime, args=(parent, runtime_id), daemon=True).start()
        threading.Thread(target=self._logs, args=(process, runtime_id), daemon=True).start()

    def _emit(self, peer, message):
        if message.get('type') == 'log':
            message = {**message, 'host_id': self.host_id, 'policy_instance_id': self.host_id,
                       'source_policy_id': self.source_policy_id, 'deployment_id': self.deployment_id,
                       'run_id': self.run_id, 'generation': self.generation}
        try: peer.send({**message, 'host_instance_id': self.instance_id})
        except (OSError, ValueError): pass

    def _broadcast(self, message):
        peers = {record['peer'] for record in self.sessions.values()}
        for peer in peers: self._emit(peer, {'runtime_id': self.runtime_id, **message})

    def _logs(self, process, runtime_id):
        try:
            for line in iter(lambda: process.stdout.readline(8192), ''):
                with self.condition:
                    if runtime_id == self.runtime_id:
                        self._broadcast({'type': 'log', 'message': line.rstrip()[:8192]})
        finally: process.stdout.close()

    def _read_runtime(self, connection, runtime_id):
        try:
            while True:
                packet = receive_message(connection)
                with self.condition:
                    if runtime_id != self.runtime_id: return
                    record = self.sessions.get(packet['session_id'])
                    if record is None: raise ValueError('unknown runtime session')
                    event = packet['event']; kind = event['type']
                    deadlines = record['deadlines']
                    if kind == 'ready':
                        self.known_children.update(descendant_snapshot(self.process.pid))
                        deadlines.pop('initialize', None)
                    if kind in ('result', 'error'): deadlines.pop('infer', None)
                    if kind in ('observed', 'observation_error'): deadlines.pop('observe', None)
                    if kind == 'ack' or (kind == 'error' and event.get('operation_id') is not None):
                        deadlines.pop('operation', None)
                    if kind == 'command_started':
                        call = record['commands'].get(event['command_id'])
                        if call and call['timeout'] != -1:
                            deadlines['command:' + str(event['command_id'])] = event['started_at'] + call['timeout']
                    if kind in ('command_result', 'command_error', 'command_cancelled'):
                        key = 'command:' + str(event['command_id'])
                        deadline = deadlines.pop(key, None)
                        record['commands'].pop(event['command_id'], None)
                        if deadline is not None and event.get('finished_at', time.monotonic()) > deadline:
                            kind = 'fatal'; event = {'type': 'fatal', 'error': 'policy command timeout'}
                    if kind == 'closed':
                        record['closed'] = True
                        record['release_error'] = event.get('release_error')
                    self._emit(record['peer'], {'type': 'policy_event', 'runtime_id': runtime_id,
                        'session_id': packet['session_id'], 'event': event})
                    self.condition.notify_all()
                if kind == 'fatal':
                    self.stop(runtime_id, timeout=0, reason=event.get('error', 'policy fatal error'))
                    return
        except (OSError, EOFError, ValueError) as exc:
            with self.condition:
                active = runtime_id == self.runtime_id and not self.closing
            if active: self.stop(runtime_id, timeout=0, reason=f'runtime disconnected: {exc}')

    def _watch(self):
        while not self.shutting_down:
            time.sleep(.02)
            with self.condition:
                if self.process is None or self.closing: continue
                self.known_children.update(descendant_snapshot(self.process.pid))
                expired = next((key for r in self.sessions.values() for key, deadline in r['deadlines'].items()
                                if time.monotonic() >= deadline), None)
                reason = (f'policy {expired} timeout' if expired else
                          'policy runtime exited' if self.process.poll() is not None else
                          'RC heartbeat expired' if time.monotonic() - self.owner_seen > 10 else None)
                runtime_id = self.runtime_id
            if reason: self.stop(runtime_id, timeout=0, reason=reason)

    def _begin_stop(self, peer, message):
        """Close admission synchronously; only the slow cleanup runs elsewhere."""
        timeout = message.get('release_timeout')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError('invalid release timeout')
        with self.condition:
            if self.owner is not None and self.owner != peer.owner:
                raise ValueError('host owned by another RC')
            runtime_id = message.get('runtime_id') or (self.runtime_id if self.owner == peer.owner else None)
            if runtime_id is not None and runtime_id not in self.stopped:
                if runtime_id != self.runtime_id:
                    raise ValueError('unknown or stale runtime stop')
                self.closing = True
            self.revoked_owners.add(peer.owner)
            return runtime_id

    def dispatch(self, peer, message):
        kind = message.get('type')
        if kind == 'hello':
            if message.get('protocol_version') != PROTOCOL_VERSION or message.get('image_protocol_version') != IMAGE_PROTOCOL_VERSION:
                raise ValueError('incompatible RPC/image protocol')
            peer.owner = message.get('owner_id')
            return {'host_id': self.host_id, 'host_instance_id': self.instance_id,
                    'protocol_version': PROTOCOL_VERSION, 'image_protocol_version': IMAGE_PROTOCOL_VERSION,
                    'python': sys.version.split()[0], 'policy_id': self.host_id,
                    'deployment_id': self.deployment_id, 'run_id': self.run_id, 'generation': self.generation,
                    'healthy': not (self.closing or self.shutting_down or self.cleanup_failed)}
        if not peer.owner: raise ValueError('hello required')
        if kind == 'ping':
            if self.owner == peer.owner:
                self.owner_seen = time.monotonic()
            return {'healthy': not (self.shutting_down or self.closing or self.cleanup_failed)}
        if kind == 'preflight': return self.preflight(message['policy_id'])
        if kind == 'stop':
            runtime_id = self._begin_stop(peer, message)
            if runtime_id is None:
                return {'runtime_id':None, 'host_instance_id':self.instance_id, 'cleanup_confirmed':True, 'forced':False}
            return self.stop(runtime_id, order=message.get('sessions'), timeout=message['release_timeout'])
        with self.condition:
            if self.owner is not None and self.owner != peer.owner: raise ValueError('host owned by another RC')
            closing_ack = (kind == 'dispatch' and message.get('event', {}).get('type') == 'global_state_ack'
                           and message.get('runtime_id') == self.runtime_id
                           and message.get('session_id') in self.sessions
                           and self.sessions[message['session_id']]['peer'] is peer
                           and not self.sessions[message['session_id']]['closed'])
            if (self.closing or self.shutting_down or self.cleanup_failed) and not closing_ack:
                raise RuntimeError('host is closing or poisoned')
            if kind == 'load':
                if peer.owner in self.revoked_owners: raise RuntimeError('RC owner revoked; create a new session')
                directory = self.preflight(message['policy_id'])['directory']
                from .catalog import validate_runtime_policy
                validate_runtime_policy(message['event']['config'], self.source_policy_id, self.host_id)
                self.owner_seen = time.monotonic()
                if self.process is None: self._start(peer.owner)
                sid = message['session_id']
                if sid in self.sessions or any(r['policy_id'] == message['policy_id'] for r in self.sessions.values()):
                    raise ValueError('policy already loaded in this runtime')
                self.sessions[sid] = dict(peer=peer, policy_id=message['policy_id'],
                    closed=False, release_error=None, timeouts=message['timeouts'], commands={},
                    deadlines={'initialize': time.monotonic() + message['timeouts']['initialize']})
                self._send_runtime({'type': 'load', 'session_id': sid, 'directory': directory, 'event': message['event']})
                return {'runtime_id': self.runtime_id, 'pid': self.process.pid}
            if kind != 'dispatch' or message.get('runtime_id') != self.runtime_id:
                raise ValueError('unknown or stale runtime request')
            record = self.sessions[message['session_id']]
            if record['closed']: raise RuntimeError('policy session closed')
            event = message['event']; event_kind = event['type']
            if event_kind in ('infer', 'observe', 'update_config', 'reset_context'):
                key = event_kind if event_kind in ('infer', 'observe') else 'operation'
                timeout = message.get('operation_timeout_s', record['timeouts']['inference'])
                if isinstance(timeout, bool) or not isinstance(timeout, (int,float)) or not math.isfinite(timeout) or timeout <= 0:
                    raise ValueError('invalid operation timeout')
                record['deadlines'][key] = time.monotonic() + timeout
            if event_kind == 'policy_command':
                record['commands'][event['command_id']] = {'timeout': message['command_timeout']}
            self._send_runtime({'type': 'dispatch', 'session_id': message['session_id'], 'event': event})

    def stop(self, runtime_id, order=None, timeout=30, reason=None):
        with self.stop_lock:
            with self.condition:
                if runtime_id in self.stopped: return self.stopped[runtime_id]
                if runtime_id != self.runtime_id or self.process is None:
                    raise RuntimeError('old runtime exit cannot be confirmed by this host')
                self.closing = True
                process = self.process
                self.known_children.update(descendant_snapshot(process.pid))
                forced = reason is not None
                if reason: self._broadcast({'type': 'host_fault', 'error': reason})
                ordered = list(dict.fromkeys((order or []) + list(reversed(self.sessions))))
                deadline = time.monotonic() + max(0, min(float(timeout), 600))
            try:
                try:
                    for sid in ordered:
                        with self.condition:
                            if sid not in self.sessions: continue
                            record = self.sessions[sid]
                            if not record['closed'] and time.monotonic() < deadline:
                                self._send_runtime({'type': 'dispatch', 'session_id': sid, 'event': {'type': 'shutdown'}})
                            while not record['closed'] and process.poll() is None and time.monotonic() < deadline:
                                self.condition.wait(.02)
                            forced |= not record['closed'] or bool(record['release_error'])
                    if not forced:
                        self._send_runtime({'type': 'exit'})
                        process.wait(timeout=1)
                except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
                    # Backpressure/disconnection cannot bypass OS-level cleanup.
                    forced = True
                cleaned = _cleanup(process, self.known_children)
                with self.condition:
                    if not cleaned: raise RuntimeError('runtime descendants remain; image readers unconfirmed')
                    result = {'runtime_id': runtime_id, 'host_instance_id': self.instance_id,
                              'cleanup_confirmed': True, 'forced': bool(forced), 'error': reason}
                    self.stopped[runtime_id] = result
                    while len(self.stopped) > 64: self.stopped.popitem(last=False)
                    self._broadcast({'type': 'runtime_stopped', **result})
                    try: self.runtime.shutdown(socket.SHUT_RDWR)
                    except OSError: pass
                    self.runtime.close()
                    try: self.runtime_outbox.put_nowait(None)
                    except queue.Full: pass
                    self.process = self.runtime = self.runtime_id = self.owner = None
                    self.sessions.clear()
                    return result
            finally:
                with self.condition:
                    self.cleanup_failed = self.process is not None
                    self.closing = self.cleanup_failed
                    self.condition.notify_all()

    def serve_peer(self, connection):
        peer = Peer(connection)
        stop_pending = threading.Event()
        def stop_request(message):
            try:
                result = self.dispatch(peer, message)
                peer.send({'type': 'rpc_result', 'rpc_id': message['rpc_id'], 'result': result})
            except Exception as exc:
                try:
                    peer.send({'type': 'rpc_result', 'rpc_id': message['rpc_id'], 'error': str(exc)})
                except OSError:
                    pass
            finally:
                stop_pending.clear()
        try:
            while True:
                message = receive_message(connection)
                try:
                    if message.get('type') == 'stop' and 'rpc_id' in message:
                        if stop_pending.is_set():
                            raise RuntimeError('stop already pending')
                        self._begin_stop(peer, message)
                        stop_pending.set()
                        threading.Thread(target=stop_request, args=(message,), name='host-stop', daemon=True).start()
                        continue
                    result = self.dispatch(peer, message)
                    if 'rpc_id' in message:
                        peer.send({'type': 'rpc_result', 'rpc_id': message['rpc_id'], 'result': result})
                except Exception as exc:
                    if message.get('type') == 'dispatch' and message.get('event', {}).get('type') == 'monitor':
                        event = message['event']
                        self._emit(peer, {'type': 'policy_event', 'runtime_id': message.get('runtime_id'),
                            'session_id': message.get('session_id'), 'event': {'type': 'monitor_error',
                            **{key: event.get(key) for key in MONITOR_IDENTITY}, 'error': str(exc)[:1024]}})
                        continue
                    if 'rpc_id' in message:
                        peer.send({'type': 'rpc_result', 'rpc_id': message['rpc_id'], 'error': str(exc)})
                    else:
                        with self.condition: runtime_id = self.runtime_id
                        if runtime_id: self.stop(runtime_id, timeout=0, reason=f'RPC rejected: {exc}')
        except (OSError, EOFError, ValueError): pass
        finally:
            peer.close(); connection.close()
            with self.condition:
                owned = self.owner == peer.owner and any(r['peer'] is peer for r in self.sessions.values())
                runtime_id = self.runtime_id
            if owned and runtime_id:
                self.stop(runtime_id, timeout=2, reason='RC connection lost')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--socket', required=True)
    parser.add_argument('--policy-root', required=True)
    parser.add_argument('--host-id', required=True)
    parser.add_argument('--source-policy-id')
    parser.add_argument('--healthcheck', action='store_true')
    args = parser.parse_args()
    if args.healthcheck:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(2); connection.connect(args.socket)
            send_message(connection, {'type':'hello','rpc_id':1,'owner_id':uuid.uuid4().hex,
                'protocol_version':PROTOCOL_VERSION,'image_protocol_version':IMAGE_PROTOCOL_VERSION})
            answer = receive_message(connection)
            if answer.get('error') or answer['result']['host_id'] != args.host_id or not answer['result'].get('healthy'): raise SystemExit(1)
        return
    become_subreaper()
    path = Path(args.socket); path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path) + '.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path.unlink(missing_ok=True)
        host = PolicyHost(args.policy_root, args.host_id, source_policy_id=args.source_policy_id)
        stop = threading.Event()
        for signum in (signal.SIGINT, signal.SIGTERM): signal.signal(signum, lambda *_: stop.set())
        with socket.socket(socket.AF_UNIX) as listener:
            listener.bind(str(path)); os.chmod(path, 0o600); listener.listen(16); listener.settimeout(.2)
            try:
                while not stop.is_set():
                    try: connection, _ = listener.accept()
                    except socket.timeout: continue
                    threading.Thread(target=host.serve_peer, args=(connection,), daemon=True).start()
            finally:
                host.shutting_down = True
                try:
                    if host.runtime_id and host.stop(host.runtime_id, timeout=2)['forced']:
                        raise SystemExit(1)
                finally:
                    path.unlink(missing_ok=True)


if __name__ == '__main__': main()
