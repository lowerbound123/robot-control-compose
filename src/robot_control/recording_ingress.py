"""Bounded recorder admission. Never acquires the writer/backend lock.

The admission lock only protects references, counters and routing state. The
backend may take admission briefly, but admission must never call the backend.
Snapshots, size walks, formatting, callbacks and I/O belong outside this lock.
"""
from collections import deque
from concurrent.futures import Future
import copy
import threading
import time
import uuid


def estimated_bytes(value):
    """Conservative retained/IPC/encoder credit, not just input array size."""
    if hasattr(value, 'nbytes'):
        return int(value.nbytes)*6 + 256
    if isinstance(value, dict):
        return 256 + sum(estimated_bytes(k)+estimated_bytes(v) for k,v in value.items())
    if isinstance(value, (list,tuple)):
        return 128 + sum(estimated_bytes(v) for v in value)
    if isinstance(value, (str,bytes)):
        return 128 + len(value)*8
    return 128


class RecordingIngress:
    def _init_ingress(self):
        self._admission = threading.RLock()
        self._ingress = deque()
        self._ingress_pending = 0
        self._ingress_serial = 0
        self._debug_pending = 0
        self._command_pending = set()
        self._abort = None
        self._gaps = {}
        self._receipt_count = 0

    def status(self):
        with self._admission:
            return {'state':self._state, 'session_id':self._session, 'path':self._path,
                    'error':self._error, 'pending_bytes':self._bytes['sample']+self._bytes['extra']+self._command_bytes,
                    'pending_samples':self._counts['sample'], 'ingress_pending':self._ingress_pending,
                    **self._stats}

    @property
    def active(self):
        with self._admission:
            return self._state == 'recording'

    def command(self, action, metadata=None):
        if action not in ('toggle','on','off','status'):
            raise ValueError('用法：record [on|off|status]')
        future = Future()
        if action == 'status':
            future.set_result(self.status())
            return future
        try:
            payload = copy.deepcopy(metadata or {})
            size = estimated_bytes(payload)
        except Exception as exc:
            future.set_exception(exc)
            return future
        error = None
        with self._admission:
            if self._closing or self._manager is None or self._quit.is_set() or self._writer_failed:
                error = 'recording service is not available'
            elif len(self._command_pending) >= 256:
                error = 'recording command queue is full'
            elif size+self._bytes['extra']+self._command_bytes > self.extra_budget:
                error = 'recording metadata budget exceeded'
            else:
                self._command_pending.add(future)
                self._command_bytes += size
                self._command_sizes[future] = size
                self._commands.append((action, payload, future))
        if error:
            future.set_exception(RuntimeError(error))
        else:
            future.add_done_callback(self._command_done)
        return future

    def _command_done(self, future):
        with self._admission:
            # Cancelling a Future does not remove its queued command or credit.
            if future not in self._command_sizes:
                self._command_pending.discard(future)

    def _drop_locked(self, session, reason, debug=False):
        # A stop fence can coexist with outside-session base logs. Keep their
        # counters separate, including failures in already accepted entries.
        if session is None or session == self._session:
            self._stats['extra_dropped'] += 1
            if debug: self._stats['debug_dropped'] += 1
        gap = self._gaps.get(session)
        if gap is None:
            gap = self._gaps[session] = {'count': 0, 'reasons': {}}
        gap['count'] = min(2**63-1, gap['count']+1)
        gap['reasons'][reason] = gap['reasons'].get(reason, 0)+1

    def _enqueue(self, kind, data, **options):
        measured = self.performance is not None and data.get('event') != 'performance'
        started = time.monotonic_ns() if measured else 0
        timings = {} if measured else None
        accepted = False
        try:
            accepted = self._enqueue_data(kind, data, timings=timings, **options)
            return accepted
        finally:
            if measured:
                finished = time.monotonic_ns()
                self.performance.emit('record.enqueue', timing_version=2, kind=kind,
                    stream=options.get('stream'), accepted=accepted,
                    started_ns=started, finished_ns=finished, duration_ns=finished-started,
                    sample_id=data.get('sample_id'), observation_id=data.get('observation_id'),
                    request_id=data.get('request_id'), instance_id=data.get('instance_id'),
                    generation=data.get('generation'), config_version=data.get('config_version'),
                    chain_request_id=data.get('chain_request_id'), event_name=data.get('event'),
                    attempt_id=data.get('attempt_id'), arm=data.get('arm'), **timings)

    def _enqueue_data(self, kind, data, *, frames=None, stream=None, priority=False,
                      session=None, owned=False, debug=False, timings=None):
        waiting = time.monotonic_ns() if timings is not None else 0
        with self._admission:
            if timings is not None:
                timings['lock_wait_ns'] = time.monotonic_ns()-waiting
            if self._quit.is_set() or self._writer_failed or self._closing:
                return False
            if kind != 'log' and (self._state != 'recording' or
                    session is not None and session != self._session):
                return False
        # Exactly one metadata snapshot, or transfer an already owned snapshot.
        # Readonly pixels are references, never recursively copied here.
        measured = time.monotonic_ns() if timings is not None else 0
        payload = data if owned or kind == 'sample' else copy.deepcopy(data)
        if frames is not None:
            frames = {name: {key: value if key == 'image' else copy.deepcopy(value)
                            for key, value in frame.items()} for name, frame in frames.items()}
        if timings is not None: timings['copy_ns'] = time.monotonic_ns()-measured
        measured = time.monotonic_ns() if timings is not None else 0
        size = estimated_bytes(payload)+estimated_bytes(frames)
        if debug:
            # Reserve the bounded strings added by background log formatting.
            size += 4096
        if timings is not None: timings['estimate_ns'] = time.monotonic_ns()-measured
        lane = 'sample' if kind == 'sample' else 'extra'
        limit = self.sample_budget if lane == 'sample' else self.extra_budget
        if lane == 'extra' and not priority: limit = limit*9//10
        count_limit = self.max_samples if lane == 'sample' else (4096 if priority else 3686)
        # Allocate containers before acquiring admission. Binding the route and
        # counters below then needs only fixed-size reference updates.
        credit = [lane, size, None, debug]
        entry = dict(kind=kind, data=payload, frames=frames, stream=stream,
            session=None, credit=credit, priority=priority, debug=debug,
            sequence=0, queued_ns=0)
        waiting = time.monotonic_ns() if timings is not None else 0
        with self._admission:
            acquired = time.monotonic_ns() if timings is not None else 0
            if timings is not None:
                timings['acquired_ns'] = acquired
                timings['lock_wait_ns'] += acquired-waiting
            if self._quit.is_set() or self._writer_failed or self._closing:
                return False
            if kind != 'log' and (self._state != 'recording' or
                    session is not None and session != self._session):
                return False
            session = self._session if self._state == 'recording' else None
            used = self._bytes[lane]+(self._command_bytes if lane == 'extra' else 0)
            if (used+size > limit or self._counts[lane] >= count_limit or
                    debug and self._debug_pending >= 2048):
                if lane == 'sample': self._fail_record('mandatory recording budget exceeded')
                else: self._drop_locked(session, 'debug_capacity' if debug else 'diagnostic_capacity', debug)
                return False
            self._bytes[lane] += size
            self._counts[lane] += 1
            self._debug_pending += int(debug)
            self._ingress_serial += 1
            if timings is not None:
                timings['record_sequence'] = self._ingress_serial
                timings['record_session_id'] = session
            credit[2] = entry['session'] = session
            entry['sequence'] = self._ingress_serial
            entry['queued_ns'] = time.monotonic_ns()
            self._ingress.append(entry)
            self._ingress_pending += 1
            self._stats['peak_pending_bytes'] = max(self._stats['peak_pending_bytes'],
                self._bytes['sample']+self._bytes['extra']+self._command_bytes)
            if lane == 'sample': self._stats['samples_accepted'] += 1
        if timings is not None: timings['work_ns'] = time.monotonic_ns()-acquired
        return True

    def log(self, entry):
        try:
            return self._enqueue('log', entry, priority='llm_failure.' in str(entry.get('message','')))
        except Exception:
            with self._admission:
                session = self._session if self._state == 'recording' else None
                self._drop_locked(session, 'snapshot_failed')
            return False

    def performance_entry(self, entry):
        return self._enqueue_data('log', entry, owned=True, debug=True)

    def event(self, stream, data, *, frames=None, priority=False, owned=False):
        try:
            return self._enqueue('event', data, frames=frames, stream=stream,
                                 priority=priority, owned=owned)
        except Exception:
            with self._admission:
                if self._state == 'recording':
                    self._drop_locked(self._session, 'snapshot_failed')
            return False

    def policy_event(self, data):
        if data.get('event') == 'policy.stdout':
            return self.event('events', data, priority='llm_failure.' in str(data.get('message','')))
        return self.event('policy', data)

    def policy_event_owned(self, data):
        # _emit_inference_trace already snapshots every mutable container.
        if data.get('event') == 'policy.stdout': return self.policy_event(data)
        return self.event('policy', data, owned=True)

    def control_attempt(self, data):
        identity = uuid.uuid4().hex
        self._attempt = identity  # Only the control thread creates attempts.
        self.event('control', {'event':'control.attempt','attempt_id':identity,
                   'timestamp_ns':time.time_ns(),'monotonic_ns':time.monotonic_ns(), **data}, priority=True)
        return identity

    def sent(self, event):
        event = {**event, 'values':list(event.get('values7', event.get('values', ()))),
                 'attempt_id':event.get('attempt_id', self._attempt)}
        with self._admission:
            if self._state != 'recording' or event['monotonic_ns'] < self._boundary: return
            if self._receipt_count >= 4096:
                self._fail_record('control sampling history overflow')
                return
            self._sent.append(event)
            self._receipt_count += 1
        self.event('control', {'event':'control.published', **event}, priority=True)

    def _fail_record(self, message):
        # Admission may fail while the manager is stalled. Fence immediately;
        # the manager handles abort I/O and notification independently.
        with self._admission:
            if self._state not in ('recording','starting','stopping'): return
            self._state, self._error = 'error', message
            self._stop_deadline = time.monotonic()+self.stop_timeout
            self._abort = (self._session, message, self._stop_deadline)
