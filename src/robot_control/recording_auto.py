"""Bounded automatic recording transitions, independent of control/lifecycle work."""
from collections import deque
from concurrent.futures import CancelledError, Future
import threading
import time


AUTO_CAPACITY = 256


class RecordingAuto:
    def _init_record_auto(self):
        self._record_auto = False
        self._record_auto_error = None
        self._record_auto_error_serial = 0
        self._record_auto_epoch = 0
        self._record_stop_epoch = 0
        self._record_auto_operations = {}
        self._record_auto_queue = deque()
        self._record_auto_current = None
        self._record_auto_thread = None
        self._record_auto_wake = threading.Event()
        # Auto rollover and connector boundaries share one transition owner.
        # Manual stop deliberately bypasses this lock and supersedes old intent.
        self._record_transition_lock = threading.Lock()

    def _record_status(self, status=None):
        with self._lock:
            return {**(self._recorder.status() if status is None else status), 'auto': self._record_auto,
                    'auto_pending': len(self._record_auto_queue) + int(self._record_auto_current is not None),
                    'auto_error': self._record_auto_error}

    def _record_operation_event(self, event):
        self._recorder.event('events', {'event':'command.receipt', 'run_id':self.run_id, **event}, priority=True)
        with self._lock:
            identity, status = event['operation_id'], event['status']
            if status == 'queued':
                if self._record_auto and not self._stop.is_set():
                    if len(self._record_auto_operations) >= AUTO_CAPACITY:
                        self._record_auto_failed('automatic recording operation tracking capacity exceeded')
                    else:
                        self._record_auto_operations[identity] = {
                            'operation_id':identity, 'auto_epoch':self._record_auto_epoch,
                            'stop_epoch':self._record_stop_epoch}
                return
            if status not in ('succeeded','failed','cancelled'):
                return
            task = self._record_auto_operations.pop(identity, None)
            if (status != 'succeeded' or task is None or not task.get('reason') or task.get('handled')
                    or not self._record_auto or self._stop.is_set()
                    or task['auto_epoch'] != self._record_auto_epoch
                    or task['stop_epoch'] != self._record_stop_epoch):
                return
            if len(self._record_auto_queue) + int(self._record_auto_current is not None) >= AUTO_CAPACITY:
                self._record_auto_failed('automatic recording rotation queue capacity exceeded')
                return
            self._record_auto_queue.append(task)
            self._record_auto_wake.set()

    def _record_auto_mark(self, identity, reason):
        task = self._record_auto_operations.get(identity)
        if task is not None:
            task['reason'] = reason

    def _record_auto_handled(self, identity):
        task = self._record_auto_operations.get(identity)
        if task is not None:
            task['handled'] = True

    def _set_record_auto(self, enabled):
        if enabled != self._record_auto:
            self._record_auto_epoch += 1
            self._record_auto_operations.clear()
        self._record_auto = enabled
        if enabled:
            self._ensure_record_auto_worker()
        else:
            # Explicit enabling also requested recording. Disabling automatic
            # cuts must not undo that pending manual start intent.
            self._clear_record_auto_queue(preserve_ensure=True)
        self._record_auto_wake.set()

    def _record_manual_stop(self):
        self._record_stop_epoch += 1
        self._record_auto_operations.clear()
        self._clear_record_auto_queue()
        self._record_auto_wake.set()

    def _clear_record_auto_queue(self, *, preserve_ensure=False):
        tasks = list(self._record_auto_queue)
        self._record_auto_queue.clear()
        if preserve_ensure:
            self._record_auto_queue.extend(task for task in tasks if task.get('ensure'))
        for task in tasks:
            if preserve_ensure and task.get('ensure'): continue
            future = task.get('completion')
            if future is not None and not future.done():
                future.set_exception(CancelledError('recording intent superseded'))

    def _record_auto_ensure(self, identity):
        future = Future()
        if len(self._record_auto_queue) + int(self._record_auto_current is not None) >= AUTO_CAPACITY:
            future.set_exception(RuntimeError('automatic recording rotation queue capacity exceeded'))
        else:
            self._record_auto_queue.append(dict(operation_id=identity, reason='auto.on', ensure=True,
                auto_epoch=self._record_auto_epoch, stop_epoch=self._record_stop_epoch, completion=future))
            self._record_auto_wake.set()
        return future

    def _record_auto_failed(self, error):
        self._record_auto_error_serial += 1
        self._record_auto_error = str(error)
        self._record_error('自动换段失败：' + str(error))

    def _ensure_record_auto_worker(self):
        if self._record_auto_thread is None:
            self._record_auto_thread = threading.Thread(target=self._record_auto_loop,
                                                        name='record-auto', daemon=True)
            self._record_auto_thread.start()

    def _record_auto_loop(self):
        try:
            while not self._stop.is_set():
                # A bounded acquire also lets shutdown join us while holding
                # the controller lock; no disk wait ever happens under it.
                if self._lock.acquire(timeout=.02):
                    try: self._record_auto_step()
                    finally: self._lock.release()
                self._record_auto_wake.wait(.02 if self._record_auto_current is not None or self._record_auto_queue else None)
                self._record_auto_wake.clear()
        finally:
            # Once stop is set no producer may enqueue a rotation. Release our
            # transition owner without needing the controller shutdown lock.
            if self._record_auto_current is not None:
                self._record_auto_current['released'] = True
                self._record_transition_lock.release()
            # Shutdown settles any public enable waiters on the controller
            # thread, after joining us, so callbacks cannot deadlock its lock.

    def _record_auto_done(self, *, status=None, error=None):
        task = self._record_auto_current
        self._record_auto_current = None
        if not task.get('released'):
            self._record_transition_lock.release()
        completion = task.get('completion')
        if completion is not None and not completion.done():
            if error is not None: completion.set_exception(error)
            else: completion.set_result(self._recorder.status() if status is None else status)
        self._record_auto_wake.set()

    def _record_auto_start(self, task):
        if (self._stop.is_set() or task['stop_epoch'] != self._record_stop_epoch
                or not (self._record_auto or (task['restore'] or task.get('ensure')) and self._record_requested)):
            self._record_auto_done(error=CancelledError('recording intent superseded'))
            return
        self._record_requested = True
        self._record_global_version = None
        metadata = self._record_metadata()
        metadata['record_auto'] = {key:task[key] for key in ('operation_id','reason','previous_session_id')}
        task['phase'] = 'starting'
        task['future'] = self._recorder.command('on', metadata)
        task['future'].add_done_callback(lambda _: self._record_auto_wake.set())

    def _record_auto_step(self):
        """One nonblocking step, under the controller lock, on the auto thread."""
        task = self._record_auto_current
        if self._stop.is_set():
            self._clear_record_auto_queue()
            if task is not None: self._record_auto_done(error=CancelledError('controller is shutting down'))
            return
        if task is not None:
            if not task['future'].done():
                return
            try:
                status = task['future'].result()  # Already done: never waits.
                if task['phase'] == 'stopping':
                    self._record_auto_start(task)
                else:
                    if task['stop_epoch'] == self._record_stop_epoch:
                        if task['error_serial'] == self._record_auto_error_serial:
                            self._record_auto_error = None
                        if not task.get('ensure'):
                            self._emit_command(f"record auto 已换段；操作 {task['operation_id']}；目录 {status['path']}")
                    self._record_auto_done(status=status)
            except Exception as exc:
                self._record_auto_done(error=exc)
                self._record_auto_failed(exc)
            return
        if not self._record_auto_queue or not (self._record_auto or self._record_auto_queue[0].get('ensure')):
            return
        if self._connector_record_transition or self._connector_retry_available and not self._connected:
            return
        status = self._recorder.status()
        if status['state'] in ('starting','stopping') or not self._record_transition_lock.acquire(blocking=False):
            return
        task = self._record_auto_queue.popleft()
        task.update(previous_session_id=status.get('session_id'), restore=self._recorder.active,
                    error_serial=self._record_auto_error_serial)
        self._record_auto_current = task
        try:
            if task.get('ensure') and task['restore']:
                self._record_auto_done(status=status)
            elif task['restore']:
                self._recorder.event('session', {'event':'record.auto.rotate',
                    **{key:task[key] for key in ('operation_id','reason','previous_session_id')}}, priority=True)
                task['phase'] = 'stopping'
                task['future'] = self._recorder.command('off')
                task['future'].add_done_callback(lambda _: self._record_auto_wake.set())
            else:
                self._record_auto_start(task)
        except Exception as exc:
            self._record_auto_done(error=exc)
            self._record_auto_failed(exc)

    def _record_auto_connector_wait(self, job):
        # Connector lifecycle may wait; control/UI threads never do. No
        # controller lock is held while waiting for a preceding auto rollover.
        while not self._record_transition_lock.acquire(timeout=min(.02, self._connector_remaining(job))):
            with self._lock: self._check_connector_job(job)
        try:
            with self._lock:
                self._check_connector_job(job)
                job['record_transition_owned'] = True
        except BaseException:
            self._record_transition_lock.release()
            raise

    def _record_auto_connector_release(self, job):
        if job.pop('record_transition_owned', False):
            self._record_transition_lock.release()
            self._record_auto_wake.set()

    def _close_record_auto(self, deadline):
        self._record_auto_wake.set()
        thread = self._record_auto_thread
        if thread is not None:
            thread.join(max(0., deadline-time.monotonic()))
            if thread.is_alive():
                self.exit_code = 1
                self._emit('自动录制线程未在退出期限内结束', 'error')
                return
        self._clear_record_auto_queue()
        self._record_auto_operations.clear()
        if self._record_auto_current is not None:
            self._record_auto_done(error=CancelledError('controller is shutting down'))
