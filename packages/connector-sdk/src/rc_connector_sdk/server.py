"""Request-driven device service; one FIFO publisher, no motion scheduler."""
import copy
import json
from pathlib import Path
import queue
import socket
import threading
import time
import uuid

from .actions import validate_arm_targets
from .images import ImagePool
from .wire import receive_message, send_message


class ConnectorServer:
    def __init__(self, driver, endpoint, *, instance_id='body', journal=None, max_pending=32):
        self.driver, self.endpoint, self.instance_id = driver, str(endpoint), instance_id
        self.runtime_id = uuid.uuid4().hex
        self.stop = threading.Event()
        self.commands = queue.Queue(maxsize=max_pending)
        self.threads, self.listeners, self.connections = [], [], []
        self.pool = None
        self.leases = {}
        self._images_lock = threading.RLock()
        self._command_lock = threading.Lock()
        self._events = []
        self.journal = Path(journal) if journal else None
        self.exit_code = 0
        self._driver_closed = False
        self._bound_paths = []
        self._shutdown_deadline = None

    def _record_pool(self):
        if self.journal is not None:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.journal.with_suffix('.tmp')
            temporary.write_text(json.dumps({'instance_id': self.instance_id, 'runtime_id': self.runtime_id,
                'pool_id': self.pool.pool_id, 'leases': list(self.leases)}))
            temporary.replace(self.journal)

    def _observe(self, event):
        self._events.append(copy.deepcopy(event))

    def _publish(self):
        while True:
            item = self.commands.get()
            if item is None:
                self.commands.task_done()
                return
            connection, message = item
            targets, self._events = message.get('params'), []
            error = None
            valid = False
            try:
                targets = validate_arm_targets(targets)
                valid = True
                self.driver.execute_arms(targets)
            except Exception as exc: error = str(exc)
            published = {e['arm'] for e in self._events}
            arms = {arm: ('published' if error is None or arm in published else 'unknown' if valid else 'not_published')
                    for arm in targets if arm in ('left', 'right')} if isinstance(targets, dict) else {}
            try: watermarks = self.driver.read_state().get('sequences', {})
            except Exception: watermarks = {}
            response = {'id': message['id'], 'success': error is None, 'error': error, 'arms': arms,
                        'events': self._events, 'feedback_watermarks': watermarks}
            try: send_message(connection, response)
            except OSError: pass  # Receipt loss does not cancel already admitted work.
            finally: self.commands.task_done()

    def _command_connection(self, connection):
        if not self._command_lock.acquire(blocking=False):
            connection.close()
            return
        try:
            while not self.stop.is_set():
                message = receive_message(connection)
                if message.get('method') != 'send_targets' or type(message.get('id')) is not int:
                    raise ValueError('invalid connector command')
                # Backpressure only; never evict an older accepted command.
                while not self.stop.is_set():
                    try: self.commands.put((connection, message), timeout=.05); break
                    except queue.Full: continue
        except (EOFError, OSError, ValueError):
            pass
        finally:
            self._command_lock.release()

    def _state(self, since):
        cached = self.driver.recording_snapshot()
        try: state, error = self.driver.read_state(), None
        except Exception as exc: state, error = cached.get('state'), str(exc)
        history, gaps = {}, {}
        for arm, seq in (since or {}).items():
            if arm not in ('left', 'right') or type(seq) is not int or seq < 0:
                raise ValueError('invalid feedback cursor')
            try: samples = self.driver.feedback_since(arm, seq)
            except Exception: samples = []
            history[arm] = samples
            gaps[arm] = bool(seq and samples and samples[0]['seq'] > seq + 1)
        return {'state': state, 'state_error': error, 'history': history, 'gaps': gaps,
                'feedback_errors': cached.get('feedback_errors', {})}

    def _images(self, cameras):
        if not isinstance(cameras, list) or not cameras or len(set(cameras)) != len(cameras):
            raise ValueError('unique camera names required')
        with self._images_lock:
            snapshot = self.driver.recording_snapshot()
            descriptors, metadata, errors = {}, {}, {}
            for camera in cameras:
                if camera not in self.pool._slots: raise ValueError(f'unknown camera: {camera}')
                frame = snapshot['frames'].get(camera)
                if frame is None:
                    errors[camera] = 'missing camera frame'
                    continue
                if not self.pool.publish(camera, frame['image'], frame['timestamp']):
                    raise RuntimeError('shared image slot capacity exhausted')
                descriptors[camera] = dict(self.pool._latest[camera]['descriptor'])
                metadata[camera] = {k: v for k, v in frame.items() if k != 'image'}
                if frame.get('error'): errors[camera] = frame['error']
            lease_id = None
            if descriptors:
                lease_id = uuid.uuid4().hex
                self.leases[lease_id] = self.pool.retain(descriptors)
                self._record_pool()
            return {'images': descriptors, 'metadata': metadata, 'errors': errors, 'lease_id': lease_id}

    def _query(self, method, params):
        if method == 'describe':
            return {'protocol_version': 1, 'instance_id': self.instance_id, 'runtime_id': self.runtime_id,
                    'pool_id': self.pool.pool_id, 'image_bytes': self.pool._bytes, 'leases': len(self.leases)}
        if method == 'get_state': return self._state(params.get('since'))
        if method == 'get_images': return self._images(params.get('cameras'))
        if method == 'release_images':
            with self._images_lock:
                for identity in params.get('lease_ids', []):
                    lease = self.leases.pop(identity, None)
                    if lease is not None: lease.release()
                self._record_pool()
            return {'released': True}
        if method == 'close':
            # The client drains command receipts before requesting close.
            self._shutdown_deadline = float(params.get('deadline', time.monotonic() + 5))
            return {'clean': not self.leases}
        raise ValueError(f'unknown connector method: {method}')

    def _query_connection(self, connection):
        try:
            while not self.stop.is_set():
                request = receive_message(connection)
                try: response = {'result': self._query(request.get('method'), request.get('params', {}))}
                except Exception as exc: response = {'error': str(exc)}
                send_message(connection, response)
                if request.get('method') == 'close':
                    self.stop.set()
                    return
        except (EOFError, OSError, ValueError): pass

    def _accept(self, listener, handler):
        while not self.stop.is_set():
            try: connection, _ = listener.accept()
            except socket.timeout: continue
            except OSError: return
            connection.settimeout(None)
            self.connections.append(connection)
            thread = threading.Thread(target=handler, args=(connection,), daemon=True)
            self.threads.append(thread)
            thread.start()

    def serve(self):
        try:
            self.driver.connect(cancel_event=self.stop)
            cameras = tuple(getattr(self.driver, 'camera_topics', ('head', 'left_wrist', 'right_wrist')))
            self.pool = ImagePool(cameras=cameras or ('head',))
            self._record_pool()
            self.driver.set_command_observer(self._observe)
            worker = threading.Thread(target=self._publish, name='connector-publisher', daemon=True)
            worker.start()
            for suffix, handler in (('.command', self._command_connection), ('.query', self._query_connection)):
                listener = socket.socket(socket.AF_UNIX)
                listener.bind(self.endpoint + suffix)
                self._bound_paths.append(self.endpoint + suffix)
                Path(self.endpoint + suffix).chmod(0o600)
                listener.listen(4)
                listener.settimeout(.1)
                self.listeners.append(listener)
                thread = threading.Thread(target=self._accept, args=(listener, handler), daemon=True)
                self.threads.append(thread)
                thread.start()
            # bind() creates a socket path before listen() can accept a peer.
            # Publish readiness only after both channels are listening.
            ready = Path(self.endpoint + '.ready')
            ready.write_text(self.runtime_id)
            ready.chmod(0o600)
            self._bound_paths.append(str(ready))
            self.stop.wait()
            deadline = self._shutdown_deadline or time.monotonic() + 5
            while self.commands.unfinished_tasks and time.monotonic() < deadline: time.sleep(.005)
            if self.commands.unfinished_tasks: self.exit_code = 1
            else:
                self.commands.put(None)
                worker.join(max(0, deadline - time.monotonic()))
                self.driver.close()
                self._driver_closed = True
        finally:
            self.stop.set()
            for connection in self.connections:
                try: connection.shutdown(socket.SHUT_RDWR)
                except OSError: pass
                connection.close()
            for listener in self.listeners: listener.close()
            for path in self._bound_paths:
                Path(path).unlink(missing_ok=True)
            if self.pool is not None:
                self.pool.close()
                if self.leases: self.exit_code = 1
                elif self.journal: self.journal.unlink(missing_ok=True)
            if not self._driver_closed:
                try: self.driver.close()
                except Exception: self.exit_code = 1
        return self.exit_code
