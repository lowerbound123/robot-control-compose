"""Nonblocking producers, 30 Hz sampling and an isolated disk writer."""
from collections import deque
from concurrent.futures import Future
import copy
from datetime import datetime
import multiprocessing
from pathlib import Path
import threading
import time
import uuid

from .log_format import UTC8
from .recording_writer import writer_main


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


def completed(value=None):
    future = Future(); future.set_result(value); return future


class RecordingService:
    HZ = 30

    def __init__(self, directory, run_id, snapshot, *, on_error=None,
                 sample_budget=512*1024*1024, extra_budget=64*1024*1024, max_samples=60,
                 stop_timeout=30):
        self.directory, self.run_id, self.snapshot = Path(directory), run_id, snapshot
        self.on_error, self.stop_timeout = on_error, stop_timeout
        self.sample_budget, self.extra_budget, self.max_samples = sample_budget, extra_budget, max_samples
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._quit = threading.Event()
        self._commands = deque()
        self._command_sizes = {}
        self._command_bytes = 0
        self._lanes = {name:deque() for name in ('control','sample','priority','extra')}
        self._pending = {}
        self._futures = {}
        self._transition_waiters = {'starting': [], 'stopping': []}
        self._bytes = {'sample':0,'extra':0}
        self._counts = {'sample':0,'extra':0}
        self._serial = 0
        self._sender = self._receiver = self._process = None
        self._manager = self._feeder = self._sampler = None
        self._boot = Future()
        self._state = 'idle'
        self._session = self._path = self._error = None
        self._stats = self._new_stats()
        self._sent = deque(maxlen=4096)
        self._attempt = None
        self._stop_deadline = None
        self._forced = False
        self._writer_failed = False
        self._gap = 0
        self._gap_session = None
        self._boundary = 0
        self._closing = False
        self._worker_ready = False
        self._shutdown_future = None

    @staticmethod
    def _new_stats():
        return dict(samples_written=0, repeated_frames=0, missing_frames=0, missed_samples=0,
                    samples_accepted=0, extra_dropped=0, peak_pending_bytes=0)

    def start(self, timeout=10):
        with self._lock:
            if self._manager is not None:
                return
            self._manager = threading.Thread(target=self._manage, name='record-manager', daemon=True)
            self._feeder = threading.Thread(target=self._feed, name='record-feed', daemon=True)
            self._sampler = threading.Thread(target=self._sample_loop, name='record-sample', daemon=True)
            self._manager.start(); self._feeder.start(); self._sampler.start()
        try:
            self._boot.result(timeout)
        except BaseException:
            self.shutdown(time.monotonic()+2)
            raise

    def status(self):
        with self._lock:
            return {'state':self._state, 'session_id':self._session, 'path':self._path,
                    'error':self._error, 'pending_bytes':sum(self._bytes.values())+self._command_bytes,
                    'pending_samples':self._counts['sample'], **self._stats}

    @property
    def active(self):
        with self._lock:
            return self._state == 'recording'

    def command(self, action, metadata=None):
        if action not in ('toggle','on','off','status'):
            raise ValueError('用法：record [on|off|status]')
        if action == 'status':
            return completed(self.status())
        future = Future()
        with self._lock:
            if self._closing or self._manager is None or self._quit.is_set() or self._writer_failed:
                future.set_exception(RuntimeError('recording service is not available'))
                return future
            if len(self._commands) + len(self._futures) + sum(map(len, self._transition_waiters.values())) >= 256:
                future.set_exception(RuntimeError('recording command queue is full'))
                return future
            size = estimated_bytes(metadata or {})
            if size + self._bytes['extra'] + self._command_bytes > self.extra_budget:
                future.set_exception(RuntimeError('recording metadata budget exceeded'))
                return future
            self._command_bytes += size
            self._command_sizes[future] = size
            try:
                self._commands.append((action, copy.deepcopy(metadata or {}), future))
            except Exception as exc:
                self._command_bytes -= self._command_sizes.pop(future)
                future.set_exception(exc)
                return future
        self._wake.set()
        return future

    def _packet(self, kind, *, lane='control', **fields):
        self._serial += 1
        packet = dict(kind=kind, token=self._serial, **fields)
        self._lanes[lane].append(packet)
        self._wake.set()
        return packet

    def _enqueue(self, kind, data, *, frames=None, stream=None, priority=False, session=None,
                 frames_factory=None, frame_size=0):
        # No serialization or IPC writes on producer/control threads.
        with self._lock:
            if self._quit.is_set() or self._writer_failed or self._closing:
                return False
            if kind != 'log' and self._state != 'recording':
                return False
            session = self._session if self._state == 'recording' else None
            lane = 'sample' if kind == 'sample' else 'extra'
            size = estimated_bytes(data) + (frame_size if frames_factory else estimated_bytes(frames))
            limit = self.sample_budget if lane == 'sample' else self.extra_budget
            if lane == 'extra' and not priority:
                limit = limit * 9 // 10  # reserve room for receipts and LLM events
            count_limit = self.max_samples if lane == 'sample' else (4096 if priority else 3686)
            used = self._bytes[lane] + (self._command_bytes if lane == 'extra' else 0)
            if used+size > limit or self._counts[lane] >= count_limit:
                if lane == 'sample':
                    self._fail_record('mandatory recording budget exceeded')
                else:
                    self._stats['extra_dropped'] += 1
                    if self._gap and self._gap_session != session:
                        self._flush_gap()
                    self._gap_session = session
                    self._gap = min(2**63-1, self._gap+1)
                return False
            self._bytes[lane] += size; self._counts[lane] += 1
            # The caller owns sample snapshots and readonly images. Other
            # metadata is copied only AFTER credit admission.
            try:
                payload = data if kind == 'sample' else copy.deepcopy(data)
                if frames_factory is not None:
                    frames = frames_factory()
                packet = self._packet(kind, lane='sample' if lane=='sample' else 'priority' if priority else 'extra',
                                      data=payload, frames=frames, stream=stream, session=session)
            except BaseException:
                self._bytes[lane] -= size; self._counts[lane] -= 1
                raise
            self._pending[packet['token']] = (lane,size,session)
            self._stats['peak_pending_bytes'] = max(self._stats['peak_pending_bytes'],sum(self._bytes.values()))
            if lane == 'sample': self._stats['samples_accepted'] += 1
            return True

    def log(self, entry):
        return self._enqueue('log', entry, priority='llm_failure.' in str(entry.get('message','')))

    def event(self, stream, data, *, frames=None, priority=False, frames_factory=None, frame_size=0):
        try:
            return self._enqueue('event', data, frames=frames, stream=stream, priority=priority,
                                 frames_factory=frames_factory, frame_size=frame_size)
        except Exception:
            with self._lock:
                session = self._session if self._state == 'recording' else None
                if self._gap and self._gap_session != session:
                    self._flush_gap()
                self._gap_session = session
                self._stats['extra_dropped'] += 1; self._gap += 1
            return False

    def _flush_gap(self):
        # A fixed-size transition marker uses the control lane so a saturated
        # diagnostics lane cannot move losses into a later session. At most one
        # marker is queued at each bounded start/stop/abort transition.
        if self._gap:
            self._packet('gap', session=self._gap_session, count=self._gap)
            self._gap = 0
            self._gap_session = None

    def policy_event(self, data):
        if data.get('event') == 'policy.stdout':
            return self.event('events', data, priority='llm_failure.' in str(data.get('message', '')))
        return self.event('policy', data)

    def observation(self, identity, state, images, *, descriptors=None):
        if descriptors is not None:
            def owned_frames():
                from rc_policy_sdk.images import map_images
                frames = {}
                missing = {name:desc for name,desc in descriptors.items() if name not in images}
                # The existing inference lease protects these mappings during
                # this copy. No additional lease or borrowed view is retained.
                if missing:
                    with map_images(missing) as mapped:
                        for name in missing:
                            pixels = mapped[name].copy()
                            pixels.flags.writeable = False
                            frames[name] = {'image':pixels}
                for name,desc in descriptors.items():
                    if name in images:
                        pixels = images[name][0]
                        if pixels.flags.writeable:
                            pixels = pixels.copy(); pixels.flags.writeable = False
                        frames[name] = {'image':pixels}
                    frames[name].update(timestamp=desc['timestamp'], valid=True,
                        pool_id=desc['pool_id'], pool_frame_id=desc['frame_id'])
                return frames
            size = sum(desc['nbytes']*6+2048 for desc in descriptors.values())
            return self.event('policy', {'event':'policy.observation','observation_id':identity,'state':state},
                              frames_factory=owned_frames, frame_size=size)
        frames = {name:{'image':pixels,'timestamp':timestamp,'valid':True}
                  for name,(pixels,timestamp) in images.items()}
        return self.event('policy', {'event':'policy.observation','observation_id':identity,'state':state}, frames=frames)

    def control_attempt(self, data):
        identity = uuid.uuid4().hex
        with self._lock: self._attempt = identity
        self.event('control', {'event':'control.attempt','attempt_id':identity,
                   'timestamp_ns':time.time_ns(),'monotonic_ns':time.monotonic_ns(), **data}, priority=True)
        return identity

    def sent(self, event):
        with self._lock:
            if self._state != 'recording': return
            event = {**event, 'values':list(event.get('values7', event.get('values', ()))), 'attempt_id':self._attempt}
            if event['monotonic_ns'] < self._boundary:
                return
            if len(self._sent) == self._sent.maxlen:
                self._fail_record('control sampling history overflow')
                return
            self._sent.append(event)
        self.event('control', {'event':'control.published', **event}, priority=True)

    def _fail_record(self, message):
        if self._state not in ('recording','starting','stopping'):
            return
        self._state, self._error = 'error', message
        self._stop_deadline = time.monotonic()+self.stop_timeout
        self._flush_gap()
        self._packet('abort', session=self._session, error=message)

    def _spawn(self):
        context = multiprocessing.get_context('spawn')
        incoming, sender = context.Pipe(duplex=False)
        receiver, outgoing = context.Pipe(duplex=False)
        process = context.Process(target=writer_main, args=(incoming,outgoing,str(self.directory),self.run_id),
                                  name='rc-record-writer', daemon=True)
        process.start()
        incoming.close(); outgoing.close()
        with self._lock:
            self._sender, self._receiver, self._process = sender, receiver, process
            self._writer_failed = False; self._worker_ready = False
        self._wake.set()

    def _feed(self):
        while not self._quit.is_set():
            self._wake.wait(.02)
            with self._lock:
                sender = self._sender
                packet = next((lane.popleft() for lane in self._lanes.values() if lane), None) if sender else None
                if packet is None:
                    self._wake.clear()
            if packet is not None:
                try:
                    sender.send(packet)
                except (OSError, EOFError) as exc:
                    with self._lock:
                        if sender is self._sender:
                            self._writer_failed = True
                            self._error = f'recording writer disconnected: {exc}'
                    self._wake.set()

    def _request(self, action, metadata, future):
        with self._lock:
            self._command_bytes -= self._command_sizes.pop(future, 0)
            if self._closing or self._writer_failed:
                future.set_exception(RuntimeError('recording service is closing or unavailable')); return
            if action == 'toggle': action = 'off' if self._state in ('recording','starting','stopping') else 'on'
            if action == 'on':
                if self._state == 'recording':
                    future.set_result(self.status()); return
                if self._state == 'starting':
                    self._transition_waiters['starting'].append(future); return
                if self._state == 'stopping' or self._stop_deadline is not None:
                    future.set_exception(RuntimeError('recording transition in progress')); return
                if self._session is not None and any(entry[2] == self._session for entry in self._pending.values()):
                    future.set_exception(RuntimeError('previous recording data is still draining')); return
                if not self._worker_ready or self._writer_failed:
                    future.set_exception(RuntimeError('recording writer unavailable')); return
                size = estimated_bytes(metadata)
                if self._bytes['extra'] + self._command_bytes + size > self.extra_budget or self._counts['extra'] >= 4096:
                    future.set_exception(RuntimeError('recording metadata budget exceeded')); return
                self._flush_gap()
                self._session = uuid.uuid4().hex
                stamp = datetime.now(UTC8).strftime('%Y%m%d_%H%M%S_%f')
                self._path = str(self.directory/f'record_{stamp}_{self._session[:8]}')
                self._state, self._error = 'starting', None
                self._stats = self._new_stats(); self._sent.clear()
                self._boundary = time.monotonic_ns()
                request = uuid.uuid4().hex
                self._futures[request] = future
                self._bytes['extra'] += size; self._counts['extra'] += 1
                packet = self._packet('start', request=request, session=self._session, path=self._path, data=metadata)
                self._pending[packet['token']] = ('extra', size, self._session)
            else:
                if self._state in ('idle','error') and self._stop_deadline is None:
                    future.set_result(self.status()); return
                if self._state == 'stopping':
                    self._transition_waiters['stopping'].append(future); return
                self._state = 'stopping'
                self._flush_gap()
                request = uuid.uuid4().hex
                self._futures[request] = future
                pending = [token for token,entry in self._pending.items() if entry[2]==self._session]
                self._packet('stop', request=request, session=self._session, pending=pending)
                self._stop_deadline = time.monotonic()+self.stop_timeout

    def _message(self, message):
        future = None
        waiters = []
        with self._lock:
            kind = message['kind']
            if kind == 'ready':
                self._worker_ready = True
                if not self._boot.done(): self._boot.set_result(None)
            elif kind == 'ack':
                credit = self._pending.pop(message['token'], None)
                if credit:
                    lane,size,_ = credit
                    self._bytes[lane] -= size; self._counts[lane] -= 1
                if message.get('stats') and self._state in ('recording','stopping'):
                    self._stats.update(message['stats'])
            elif kind == 'started':
                if self._state == 'starting':
                    self._state = 'recording'; self._boundary = time.monotonic_ns()
                future = self._futures.pop(message['request'],None)
                waiters, self._transition_waiters['starting'] = self._transition_waiters['starting'], []
            elif kind == 'stopped':
                self._stats.update(message.get('stats', {}))
                self._state = 'error' if message.get('error') or self._error else 'idle'
                self._error = message.get('error') or self._error
                self._stop_deadline = None
                future = self._futures.pop(message['request'],None)
                waiters, self._transition_waiters['stopping'] = self._transition_waiters['stopping'], []
            elif kind in ('failed','aborted'):
                self._state, self._error = 'error', message['error']
                self._stop_deadline = None
                self._notify_error(message['error'])
            elif kind == 'request_error':
                self._state, self._error = 'error', message['error']
                future = self._futures.pop(message['request'],None)
                waiters = sum(self._transition_waiters.values(), [])
                self._transition_waiters = {'starting': [], 'stopping': []}
            elif kind in ('fatal','log_error'):
                self._error = message['error']
                if kind == 'fatal': self._writer_failed = True
                if kind == 'fatal' and not self._boot.done():
                    self._boot.set_exception(RuntimeError(message['error']))
                self._notify_error(message['error'])
        for pending in ([future] if future else []) + waiters:
            if not pending.done():
                if message.get('error'): pending.set_exception(RuntimeError(message['error']))
                else: pending.set_result(self.status())

    def _notify_error(self, message):
        # Never call a controller callback under the recorder lock.
        if self.on_error:
            self._commands.append(('notify', message, None))

    def _manage(self):
        try:
            self._spawn()
            while not self._quit.is_set():
                with self._lock:
                    command = self._commands.popleft() if self._commands else None
                    receiver, process = self._receiver, self._process
                    overdue = self._stop_deadline is not None and time.monotonic() >= self._stop_deadline
                if command:
                    action,data,future = command
                    if action == 'notify':
                        try: self.on_error(data)
                        except Exception: pass
                    else:
                        self._request(action,data,future)
                if receiver and receiver.poll(.01):
                    self._message(receiver.recv())
                if overdue:
                    self._terminate('recording drain deadline exceeded')
                elif process and not process.is_alive() and not self._closing:
                    self._terminate(self._error or 'recording writer exited')
                with self._lock:
                    gap = self._gap
                    gap_data = {'event':'diagnostic_gap','count':gap}
                    can_report = (self._counts['extra'] < 4096 and
                        self._bytes['extra'] + self._command_bytes + estimated_bytes(gap_data) + estimated_bytes(None) <= self.extra_budget)
                    if gap and can_report and self._state == 'recording' and self._gap_session == self._session:
                        self._gap = 0
                        self.event('session', gap_data, priority=True)
        except BaseException as exc:
            if not self._boot.done(): self._boot.set_exception(exc)
            if not self._quit.is_set() and not (self._closing and isinstance(exc, (EOFError, OSError))):
                self._terminate(str(exc))

    def _terminate(self, reason, deadline=None):
        with self._lock:
            process = self._process
            self._worker_ready = False; self._writer_failed = True
            self._state, self._error = 'error', reason
            self._stop_deadline = None
        if process and process.is_alive():
            remaining = lambda cap: cap if deadline is None else min(cap,max(0,deadline-time.monotonic()))
            grace = .2 if deadline is None else min(.05,max(0,deadline-time.monotonic())/4)
            process.terminate(); process.join(grace)
            if process.is_alive(): process.kill(); process.join(remaining(.5))
            self._forced = True
        with self._lock:
            for lane in self._lanes.values(): lane.clear()
            self._pending.clear()
            self._bytes = {'sample':0,'extra':0}; self._counts = {'sample':0,'extra':0}
            self._process = process if process is not None and process.is_alive() else None
            futures = list(self._futures.values()) + sum(self._transition_waiters.values(), [])
            self._futures.clear()
            self._command_sizes.clear(); self._command_bytes = 0
            self._transition_waiters = {'starting': [], 'stopping': []}
            while self._commands:
                action, data, future = self._commands.popleft()
                if future is not None: futures.append(future)
        for future in futures:
            if not future.done(): future.set_exception(RuntimeError(reason))
        if self.on_error:
            try: self.on_error(reason)
            except Exception: pass

    def _sample_loop(self):
        current, index = None, 0
        previous = 0
        while not self._quit.is_set():
            with self._lock:
                session = self._session if self._state == 'recording' else None
                epoch = self._boundary
            if session is None:
                self._quit.wait(.01); continue
            if session != current:
                current, index, previous = session, 0, epoch
            scheduled = epoch + index*1_000_000_000//self.HZ
            delay = (scheduled-time.monotonic_ns())/1e9
            if delay > 0:
                self._quit.wait(min(delay,.02)); continue
            now = time.monotonic_ns()
            missed = now-scheduled >= 1_000_000_000//self.HZ
            if missed:
                capture = {'state':None,'state_error':'sampler missed deadline','frames':{}}
            else:
                try: capture = self.snapshot()
                except Exception as exc: capture = {'state':None,'state_error':str(exc),'frames':{}}
            wall, cutoff = time.time_ns(), time.monotonic_ns()
            with self._lock:
                if self._state != 'recording' or self._session != session: continue
                controls = {}
                if not missed:
                    while self._sent and self._sent[0]['monotonic_ns'] <= cutoff:
                        event = self._sent.popleft()
                        # An observer descheduled just after publication can
                        # arrive after the previous cutoff. Keep that real send
                        # and expose its original timestamp instead of losing it.
                        controls[event['arm']] = {**event, 'late_observer':event['monotonic_ns'] <= previous}
                    previous = cutoff
                payload = {**capture, 'sample_id':index, 'timestamp_ns':wall, 'monotonic_ns':cutoff,
                           'scheduled_ns':scheduled, 'missed':missed, 'controls':controls}
                self._enqueue('sample', payload)
            index += 1

    def shutdown(self, deadline, *, final_entry=None):
        # Reserve part of the caller's remaining absolute deadline for killing
        # and reaping a stuck writer, closing pipes and joining transport threads.
        cleanup_margin = min(.5, max(0,deadline-time.monotonic())*.4)
        with self._lock:
            if self._manager is None: return False
            failed = self._writer_failed
            if failed:
                self._closing = True
                self._flush_gap()
                future = None
            elif not self._closing:
                self._closing = True
                self._flush_gap()
                while self._commands:
                    _, _, queued = self._commands.popleft()
                    self._command_bytes -= self._command_sizes.pop(queued, 0)
                    if queued is not None and not queued.done():
                        queued.set_exception(RuntimeError('recording service is closing'))
                self._state = 'stopping' if self._state == 'recording' else self._state
                future = Future(); request = uuid.uuid4().hex
                self._shutdown_future = future
                self._futures[request] = future
                self._packet('shutdown', request=request, session=self._session, pending=list(self._pending), final_entry=final_entry)
            else:
                future = self._shutdown_future
        try:
            if failed:
                self._terminate(self._error or 'recording writer unavailable', deadline)
            elif future is not None:
                future.result(max(0,deadline-time.monotonic()-cleanup_margin))
        except Exception:
            self._terminate('recording shutdown deadline exceeded', deadline)
            self._forced = True
        self._quit.set(); self._wake.set()
        process = self._process
        if process is not None:
            process.join(max(0,deadline-time.monotonic()))
            if process.is_alive(): self._terminate('recording writer did not exit', deadline)
        for connection in (self._sender,self._receiver):
            if connection:
                try: connection.close()
                except OSError: pass
        for thread in (self._sampler,self._feeder,self._manager):
            if thread and thread is not threading.current_thread(): thread.join(max(0,deadline-time.monotonic()))
        return self._forced
