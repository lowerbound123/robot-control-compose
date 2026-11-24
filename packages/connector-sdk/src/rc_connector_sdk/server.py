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
from .performance import FeedbackTimings, mark


class ConnectorServer:
    def __init__(self, driver, endpoint, *, instance_id='body', journal=None, max_pending=32, debug_timing=False):
        self.driver, self.endpoint, self.instance_id = driver, str(endpoint), instance_id
        self.runtime_id = uuid.uuid4().hex
        self.stop = threading.Event()
        self.commands = queue.Queue(maxsize=max_pending)
        self.threads, self.listeners, self.connections = set(), [], {}
        self._connections_lock = threading.Lock()
        self.pool = None
        self.leases = {}
        self._images_lock = threading.RLock()
        self._command_lock = threading.Lock()
        self._events = []
        self.journal = Path(journal) if journal else None
        self.exit_code = 0
        self._bound_paths = []
        self._shutdown_deadline = None
        self._feedback_timing = None
        if debug_timing and hasattr(driver, 'set_feedback_observer'):
            self._feedback_timing = FeedbackTimings()
            driver.set_feedback_observer(self._feedback_timing.received)

    def _record_pool(self):
        if self.journal is not None:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.journal.with_suffix('.tmp')
            temporary.write_text(json.dumps({'instance_id': self.instance_id, 'runtime_id': self.runtime_id,
                'pool_id': self.pool.pool_id, 'leases': list(self.leases)}))
            temporary.replace(self.journal)

    def _observe(self, event):
        self._events.append(copy.deepcopy(event))

    def _publish(self, read_watermarks=None):
        while True:
            item = self.commands.get()
            if item is None:
                self.commands.task_done()
                return
            connection, message = item
            mark(message, 'connector.publisher_dequeued')
            targets, self._events = message.get('params'), []
            error = None
            valid = False
            try:
                targets = validate_arm_targets(targets)
                valid = True
                mark(message, 'connector.driver_begin')
                self.driver.execute_arms(targets)
            except Exception as exc: error = str(exc)
            mark(message, 'connector.driver_end', success=error is None)
            published = {e['arm'] for e in self._events}
            arms = {arm: ('published' if error is None or arm in published else 'unknown' if valid else 'not_published')
                    for arm in targets if arm in ('left', 'right')} if isinstance(targets, dict) else {}
            try:
                watermarks = read_watermarks() if read_watermarks else self.driver.read_state().get('sequences', {})
            except Exception: watermarks = {}
            response = {'id': message['id'], 'success': error is None, 'error': error, 'arms': arms,
                        'events': self._events, 'feedback_watermarks': watermarks}
            if '_performance' in message:
                response['_performance'] = message['_performance']
                mark(response, 'connector.receipt_ready')
            try: send_message(connection, response)
            except OSError: pass  # Receipt loss does not cancel already admitted work.
            finally:
                self._release_connection(connection)
                self.commands.task_done()

    def _command_connection(self, connection):
        if not self._command_lock.acquire(blocking=False):
            return
        try:
            while not self.stop.is_set():
                message = receive_message(connection)
                mark(message, 'connector.server_received')
                if message.get('method') != 'send_targets' or type(message.get('id')) is not int:
                    raise ValueError('invalid connector command')
                # Backpressure only; never evict an older accepted command.
                # The publisher owns a reference until the receipt is sent,
                # including when the peer has half-closed its sending side.
                with self._connections_lock:
                    if connection not in self.connections:
                        return
                    self.connections[connection] += 1
                queued = False
                try:
                    while not self.stop.is_set():
                        try:
                            self.commands.put((connection, message), timeout=.05)
                            queued = True
                            break
                        except queue.Full: continue
                finally:
                    if not queued: self._release_connection(connection)
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
        if method == 'get_state':
            result = self._state(params.get('since'))
            if self._feedback_timing is not None:
                self._feedback_timing.drain_into(result)
            return result
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
            result = {'clean': not self.leases}
            if self._feedback_timing is not None:
                self._feedback_timing.drain_into(result, closing=True)
            return result
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

    def _release_connection(self, connection):
        with self._connections_lock:
            owners = self.connections.get(connection)
            if owners is None:
                return
            if owners > 1:
                self.connections[connection] = owners - 1
                return
            del self.connections[connection]
        connection.close()

    def _serve_connection(self, connection, handler):
        try:
            handler(connection)
        finally:
            self._release_connection(connection)
            with self._connections_lock:
                self.threads.discard(threading.current_thread())

    def _accept(self, listener, handler):
        try:
            while not self.stop.is_set():
                try: connection, _ = listener.accept()
                except socket.timeout: continue
                except OSError: return
                connection.settimeout(None)
                thread = threading.Thread(target=self._serve_connection, args=(connection, handler), daemon=True)
                with self._connections_lock:
                    if self.stop.is_set():
                        connection.close()
                        return
                    self.connections[connection] = 1
                    self.threads.add(thread)
                    try:
                        thread.start()
                    except BaseException:
                        self.threads.discard(thread)
                        del self.connections[connection]
                        connection.close()
                        raise
        finally:
            with self._connections_lock:
                self.threads.discard(threading.current_thread())

    def _bind(self, suffix):
        path = self.endpoint + suffix
        listener = socket.socket(socket.AF_UNIX)
        self.listeners.append(listener)
        listener.bind(path)
        self._bound_paths.append(path)
        Path(path).chmod(0o600)
        listener.settimeout(.1)
        return listener

    def _start_accepting(self, listener, handler):
        listener.listen(4)
        thread = threading.Thread(target=self._accept, args=(listener, handler), daemon=True)
        with self._connections_lock:
            self.threads.add(thread)
        thread.start()

    def _start_commands(self, listener, read_watermarks=None):
        self.driver.set_command_observer(self._observe)
        worker = threading.Thread(target=self._publish, args=(read_watermarks,), name='connector-publisher', daemon=True)
        worker.start()
        self._start_accepting(listener, self._command_connection)
        return worker

    def _drain_commands(self, worker, deadline):
        # stop admissions before putting the sentinel behind every accepted frame.
        with self._connections_lock:
            for connection in self.connections:
                try: connection.shutdown(socket.SHUT_RD)
                except OSError: pass
        if not self._command_lock.acquire(timeout=max(0., deadline - time.monotonic())):
            return False
        self._command_lock.release()
        try: self.commands.put(None, timeout=max(0., deadline - time.monotonic()))
        except queue.Full: return False
        worker.join(max(0., deadline - time.monotonic()))
        return not worker.is_alive()

    def _close_io(self):
        for listener in self.listeners: listener.close()
        with self._connections_lock:
            connections = tuple(self.connections)
            self.connections.clear()
        for connection in connections:
            try: connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            connection.close()
        for path in self._bound_paths:
            Path(path).unlink(missing_ok=True)

    def serve_commands(self, listener, control, read_watermarks):
        """Child process. The query server owns both socket paths and readiness."""
        activated = threading.Event()

        def watch_parent():
            try:
                request = receive_message(control)
                if request == {'activate': True}:
                    activated.set()
                    request = receive_message(control)
                self._shutdown_deadline = request['deadline']
            except (EOFError, OSError, ValueError, KeyError):
                self.exit_code = 1
            finally:
                self.stop.set()
                activated.set()

        threading.Thread(target=watch_parent, daemon=True).start()
        worker = None
        try:
            self.driver.connect(cancel_event=self.stop)
            # ROS discovery can overlap. Admit the first command only after the
            # query process has fresh feedback and this node has both subscribers.
            while not activated.wait(.05) and not self.stop.is_set():
                pass
            if not self.stop.is_set():
                worker = self._start_commands(listener, read_watermarks)
                send_message(control, {'ready': True})
                self.stop.wait()
        finally:
            self.stop.set()
            deadline = self._shutdown_deadline or time.monotonic() + 5
            if worker and not self._drain_commands(worker, deadline):
                self.exit_code = 1
            self._close_io()
            self.driver.close(deadline=deadline)
        return self.exit_code or int(self._shutdown_deadline is None)

    def serve(self, command_process=None):
        worker = None
        try:
            if command_process is not None:
                command_process.start(self._bind('.command'), self.stop)
            self.driver.connect(cancel_event=self.stop)
            cameras = tuple(getattr(self.driver, 'camera_topics', ('head', 'left_wrist', 'right_wrist')))
            self.pool = ImagePool(cameras=cameras or ('head',))
            self._record_pool()
            if command_process is None:
                worker = self._start_commands(self._bind('.command'))
            else:
                command_process.wait_ready(self.stop)
            self._start_accepting(self._bind('.query'), self._query_connection)
            ready = Path(self.endpoint + '.ready')
            ready.write_text(self.runtime_id)
            self._bound_paths.append(str(ready))
            ready.chmod(0o600)
            self.stop.wait()
        finally:
            self.stop.set()
            deadline = self._shutdown_deadline or time.monotonic() + 5
            try:
                if command_process is not None:
                    if not command_process.close(deadline): self.exit_code = 1
                elif worker and not self._drain_commands(worker, deadline):
                    self.exit_code = 1
            finally:
                self._close_io()
                try:
                    if self.pool is not None:
                        self.pool.close()
                        if self.leases: self.exit_code = 1
                        elif self.journal: self.journal.unlink(missing_ok=True)
                finally:
                    try:
                        if command_process is None: self.driver.close()
                        else: self.driver.close(deadline=deadline)
                    except Exception: self.exit_code = 1
        return self.exit_code
