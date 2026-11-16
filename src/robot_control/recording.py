"""Nonblocking producers, 30 Hz sampling and an isolated disk writer."""
from collections import deque
from concurrent.futures import Future
from datetime import datetime
import multiprocessing
from multiprocessing.connection import wait as wait_connections
import os
import signal
from pathlib import Path
import threading
import time
import uuid

from .log_format import UTC8, format_log_time
from .recording_ingress import RecordingIngress
from .recording_writer import writer_main

def _writer_entry(function, *args):
    # FFmpeg inherits this dedicated group, allowing deadline cleanup even if
    # the writer is stopped or has already crashed.
    os.setsid()
    function(*args)


def _signal_writer_group(process, signum):
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass
    except PermissionError:
        # Darwin returns EPERM for a group containing only an unreaped zombie.
        # is_alive() can briefly be stale while another thread reaps waitpid;
        # the spawn sentinel independently proves that the writer has exited.
        if not wait_connections([process.sentinel], timeout=0): raise


class RecordingService(RecordingIngress):
    HZ = 30

    def __init__(self, directory, run_id, snapshot, *, on_error=None,
                 sample_budget=512*1024*1024, extra_budget=64*1024*1024, max_samples=60,
                 stop_timeout=30):
        self.directory, self.run_id, self.snapshot = Path(directory), run_id, snapshot
        self.on_error, self.stop_timeout = on_error, stop_timeout
        self.sample_budget, self.extra_budget, self.max_samples = sample_budget, extra_budget, max_samples
        self._lock = threading.RLock()
        self._init_ingress()
        self._preparing = deque()
        self._wake = threading.Event()
        self._quit = threading.Event()
        self._commands = deque()
        self._command_sizes = {}
        self._command_bytes = 0
        self._lanes = {name:deque() for name in ('control','sample','priority','extra','debug')}
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
        self._boundary = 0
        self._closing = False
        self._worker_ready = False
        self.performance = None
        self._shutdown_future = None

    @staticmethod
    def _new_stats():
        return dict(samples_written=0, repeated_frames=0, missing_frames=0, missed_samples=0,
                    samples_accepted=0, extra_dropped=0, peak_pending_bytes=0, debug_dropped=0)

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

    def _packet(self, kind, *, lane='control', **fields):
        self._serial += 1
        packet = dict(kind=kind, token=self._serial, **fields)
        self._lanes[lane].append(packet)
        self._wake.set()
        return packet

    def observation(self, identity, state, images, *, descriptors=None):
        # RC supplies owned readonly pixels matching the actual lease. Never
        # map a borrowed descriptor or copy pixels on the control thread.
        if not self.active: return False
        frames = {}
        selected = descriptors if descriptors is not None else images
        for name in selected:
            if name not in images or images[name][0].flags.writeable:
                with self._admission:
                    if self._state == 'recording':
                        self._drop_locked(self._session, 'observation_frame_unavailable')
                return False
            pixels, timestamp = images[name]
            frames[name] = dict(image=pixels, timestamp=timestamp, valid=True)
            if descriptors is not None:
                descriptor = descriptors[name]
                frames[name].update(timestamp=descriptor['timestamp'], pool_id=descriptor['pool_id'],
                                    pool_frame_id=descriptor['frame_id'])
        return self.event('policy', {'event':'policy.observation','observation_id':identity,'state':state},
                          frames=frames)

    def _spawn(self):
        context = multiprocessing.get_context('spawn')
        incoming, sender = context.Pipe(duplex=False)
        receiver, outgoing = context.Pipe(duplex=False)
        process = context.Process(target=_writer_entry, args=(writer_main,incoming,outgoing,str(self.directory),self.run_id),
                                  name='rc-record-writer', daemon=True)
        process.start()
        incoming.close(); outgoing.close()
        with self._lock:
            self._sender, self._receiver, self._process = sender, receiver, process
            with self._admission:
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
                            with self._admission:
                                self._writer_failed = True
                                self._error = f'recording writer disconnected: {exc}'
                    self._wake.set()

    def _release_credit(self, credit):
        lane, size, _, debug = credit
        with self._admission:
            self._bytes[lane] -= size
            self._counts[lane] -= 1
            self._debug_pending -= int(debug)

    def _prepare_entry(self, entry):
        """Manager only; formatting and backend bookkeeping never run in producers."""
        started = time.monotonic_ns()
        data = entry['data']
        if entry['debug']:
            try:
                data['time'] = format_log_time(data['timestamp_ns']/1e9)
                data['message'] = 'performance ' + data['stage']
                data['logged_monotonic_ns'] = time.monotonic_ns()
            except Exception:
                self._release_credit(entry['credit'])
                with self._admission:
                    self._ingress_pending -= 1
                    self._drop_locked(entry['session'], 'debug_format_failed', True)
                return
        lane = ('sample' if entry['kind'] == 'sample' else 'priority' if entry['priority']
                else 'debug' if entry['debug'] else 'extra')
        packet = self._packet(entry['kind'], lane=lane, data=data, frames=entry['frames'],
                              stream=entry['stream'], session=entry['session'])
        self._pending[packet['token']] = entry['credit']
        with self._admission:
            self._ingress_pending -= 1
        if self.performance is not None and not entry['debug'] and data.get('event') != 'performance':
            self.performance.emit('record.prepare', timing_version=2,
                queued_ns=entry['queued_ns'], started_ns=started,
                queue_wait_ns=started-entry['queued_ns'], prepare_ns=time.monotonic_ns()-started,
                record_session_id=entry['session'], record_sequence=entry.get('sequence'),
                kind=entry['kind'], event_name=data.get('event'), request_id=data.get('request_id'),
                observation_id=data.get('observation_id'), sample_id=data.get('sample_id'),
                attempt_id=data.get('attempt_id'), instance_id=data.get('instance_id'))

    def _drain_ingress(self, *, all_entries=False):
        # Caller owns _lock. Detach in O(1); never walk producer data under
        # admission. New producers use the replacement deque immediately.
        with self._admission:
            incoming, self._ingress = self._ingress, deque()
        self._preparing.extend(incoming)
        limit = len(self._preparing) if all_entries else min(64, len(self._preparing))
        for _ in range(limit):
            entry = self._preparing.popleft()
            self._prepare_entry(entry)

    def _flush_gap(self, *, transition=True):
        with self._admission:
            if not self._gaps: return
            # Ordinary reports consume credit, so even an unavailable writer
            # cannot cause an unbounded stream of loss reports. Transitions
            # retain one reserved marker, as before.
            size = 2048
            if not transition and (self._counts['extra'] >= 4096 or
                    self._bytes['extra']+self._command_bytes+size > self.extra_budget):
                return
            if transition:
                gaps, self._gaps = self._gaps, {}
            else:
                session = next(iter(self._gaps))
                gaps = {session: self._gaps.pop(session)}
                self._bytes['extra'] += size
                self._counts['extra'] += 1
        for session, gap in gaps.items():
            packet = self._packet('gap', session=session, **gap)
            if not transition: self._pending[packet['token']] = ('extra', size, session, False)

    def _request(self, action, metadata, future):
        result = error = None
        try:
            with self._lock:
                result = self._request_locked(action, metadata, future)
        except Exception as exc:
            error = exc
        finally:
            with self._admission:
                self._command_bytes -= self._command_sizes.pop(future, 0)
            if future.done(): self._command_done(future)
        # Future callbacks may reenter RC. They must own neither recorder lock.
        if not future.done():
            if error is not None: future.set_exception(error)
            elif result is not None: future.set_result(result)

    def _request_locked(self, action, metadata, future):
        with self._admission:
            size = self._command_sizes.get(future, 0)
            state = self._state
            unavailable = self._closing or self._writer_failed
        if unavailable: raise RuntimeError('recording service is closing or unavailable')
        if action == 'toggle': action = 'off' if state in ('recording','starting','stopping') else 'on'
        if action == 'on':
            if state == 'recording': return self.status()
            if state == 'starting':
                self._transition_waiters['starting'].append(future)
                return
            if state == 'stopping' or self._stop_deadline is not None:
                raise RuntimeError('recording transition in progress')
            self._drain_ingress(all_entries=True)
            if self._session is not None and any(entry[2] == self._session for entry in self._pending.values()):
                raise RuntimeError('previous recording data is still draining')
            if not self._worker_ready or self._writer_failed:
                raise RuntimeError('recording writer unavailable')
            identity = uuid.uuid4().hex
            stamp = datetime.now(UTC8).strftime('%Y%m%d_%H%M%S_%f')
            path = str(self.directory/f'record_{stamp}_{identity[:8]}')
            self._flush_gap()
            with self._admission:
                if self._bytes['extra']+self._command_bytes > self.extra_budget or self._counts['extra'] >= 4096:
                    raise RuntimeError('recording metadata budget exceeded')
                self._session, self._path = identity, path
                self._state, self._error = 'starting', None
                self._stats = self._new_stats()
                old_sent, self._sent = self._sent, deque(maxlen=4096)
                self._receipt_count = 0
                self._boundary = time.monotonic_ns()
                self._command_bytes -= self._command_sizes.pop(future, 0)
                self._bytes['extra'] += size
                self._counts['extra'] += 1
            request = uuid.uuid4().hex
            self._futures[request] = future
            packet = self._packet('start', request=request, session=identity, path=path, data=metadata)
            self._pending[packet['token']] = ('extra', size, identity, False)
        else:
            if state in ('idle','error') and self._stop_deadline is None: return self.status()
            if state == 'stopping':
                self._transition_waiters['stopping'].append(future)
                return
            with self._admission:
                self._state = 'stopping'
                self._stop_deadline = time.monotonic()+self.stop_timeout
            # Fence admission before registering the complete old-session queue
            # with the existing writer barrier. Base logs can still be offered.
            self._drain_ingress(all_entries=True)
            self._flush_gap()
            request = uuid.uuid4().hex
            self._futures[request] = future
            pending = [token for token,entry in self._pending.items() if entry[2] == self._session]
            self._packet('stop', request=request, session=self._session, pending=pending,
                         deadline=self._stop_deadline)

    def _message(self, message):
        future, boot, waiters = None, False, []
        with self._lock:
            kind = message['kind']
            if kind == 'ack':
                credit = self._pending.pop(message['token'], None)
                if credit: self._release_credit(credit)
            with self._admission:
                if kind == 'ready':
                    self._worker_ready = True
                    boot = True
                elif kind == 'ack':
                    if message.get('stats') and self._state in ('recording','stopping'):
                        self._stats.update(message['stats'])
                elif kind == 'started':
                    if self._state == 'starting':
                        self._state = 'recording'; self._boundary = time.monotonic_ns()
                elif kind == 'stopped':
                    self._stats.update(message.get('stats', {}))
                    self._state = 'error' if message.get('error') or self._error else 'idle'
                    self._error = message.get('error') or self._error
                    self._stop_deadline = None
                elif kind in ('failed','aborted','request_error'):
                    self._state, self._error = 'error', message['error']
                    self._stop_deadline = None
                elif kind in ('fatal','log_error'):
                    self._error = message['error']
                    if kind == 'fatal': self._writer_failed = True
            if kind in ('started','stopped'):
                future = self._futures.pop(message['request'], None)
                transition = 'starting' if kind == 'started' else 'stopping'
                waiters, self._transition_waiters[transition] = self._transition_waiters[transition], []
            elif kind == 'request_error':
                future = self._futures.pop(message['request'], None)
                waiters = sum(self._transition_waiters.values(), [])
                self._transition_waiters = {'starting': [], 'stopping': []}
            elif kind in ('failed','aborted','fatal','log_error'):
                self._notify_error(message['error'])
        if boot and not self._boot.done(): self._boot.set_result(None)
        if kind == 'fatal' and not self._boot.done(): self._boot.set_exception(RuntimeError(message['error']))
        for pending in ([future] if future else [])+waiters:
            if not pending.done():
                if message.get('error'): pending.set_exception(RuntimeError(message['error']))
                else: pending.set_result(self.status())

    def _notify_error(self, message):
        if self.on_error:
            with self._admission: self._commands.append(('notify', message, None))

    def _manage(self):
        try:
            self._spawn()
            while not self._quit.is_set():
                with self._admission:
                    command = self._commands.popleft() if self._commands else None
                    abort, self._abort = self._abort, None
                    overdue = self._stop_deadline is not None and time.monotonic() >= self._stop_deadline
                with self._lock:
                    receiver, process = self._receiver, self._process
                    if abort:
                        self._flush_gap()
                        session, error, deadline = abort
                        self._packet('abort', session=session, error=error, deadline=deadline)
                if command:
                    action, data, future = command
                    if action == 'notify':
                        try: self.on_error(data)
                        except Exception: pass
                    else: self._request(action, data, future)
                with self._lock:
                    self._drain_ingress()
                    self._flush_gap(transition=False)
                if receiver and receiver.poll(.01): self._message(receiver.recv())
                if overdue: self._terminate('recording drain deadline exceeded')
                elif process and not process.is_alive() and not self._closing:
                    self._terminate(self._error or 'recording writer exited')
        except BaseException as exc:
            if not self._boot.done(): self._boot.set_exception(exc)
            if not self._quit.is_set() and not (self._closing and isinstance(exc, (EOFError, OSError))):
                self._terminate(str(exc))

    def _terminate(self, reason, deadline=None):
        with self._lock:
            process = self._process
            with self._admission:
                self._worker_ready = False; self._writer_failed = True
                self._state, self._error = 'error', reason
                self._stop_deadline = None
        if process:
            remaining = lambda cap: cap if deadline is None else min(cap,max(0,deadline-time.monotonic()))
            grace = .2 if deadline is None else min(.05,max(0,deadline-time.monotonic())/4)
            _signal_writer_group(process, signal.SIGTERM)
            if process.is_alive():
                process.terminate(); process.join(grace)
                self._forced = True
            # The leader can exit before its encoders. Kill the owned group
            # unconditionally, including orphaned encoders after writer exit.
            _signal_writer_group(process, signal.SIGKILL)
            if process.is_alive(): process.kill()
            process.join(remaining(.5))
        with self._lock:
            for lane in self._lanes.values(): lane.clear()
            self._pending.clear()
            self._preparing.clear()
            self._process = process if process is not None and process.is_alive() else None
            futures = list(self._futures.values()) + sum(self._transition_waiters.values(), [])
            self._futures.clear()
            self._transition_waiters = {'starting': [], 'stopping': []}
            with self._admission:
                self._bytes = {'sample':0,'extra':0}; self._counts = {'sample':0,'extra':0}
                abandoned, self._ingress = self._ingress, deque()
                commands, self._commands = self._commands, deque()
                sizes, self._command_sizes = self._command_sizes, {}
                command_pending, self._command_pending = self._command_pending, set()
                self._command_bytes = self._ingress_pending = self._debug_pending = 0
            futures.extend(future for _, _, future in commands if future is not None)
            futures.extend(command_pending)
        for future in set(futures):
            if not future.done(): future.set_exception(RuntimeError(reason))
        if self.on_error:
            try: self.on_error(reason)
            except Exception: pass

    def _sample_loop(self):
        current, index, previous = None, 0, 0
        observed = deque()
        while not self._quit.is_set():
            with self._admission:
                session = self._session if self._state == 'recording' else None
                epoch = self._boundary
            if session is None:
                self._quit.wait(.01); continue
            if session != current:
                current, index, previous = session, 0, epoch
                observed.clear()
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
            with self._admission:
                acquired = time.monotonic_ns() if self.performance is not None else 0
                if self._state != 'recording' or self._session != session: continue
                batch = None
                if not missed:
                    batch, self._sent = self._sent, deque(maxlen=4096)
            controls, consumed = {}, 0
            if batch is not None:
                observed.extend(batch)
                while observed and observed[0]['monotonic_ns'] <= cutoff:
                    event = observed.popleft()
                    controls[event['arm']] = {**event, 'late_observer':event['monotonic_ns'] <= previous}
                    consumed += 1
                previous = cutoff
            with self._admission:
                if self._state != 'recording' or self._session != session: continue
                self._receipt_count -= consumed
            payload = {**capture, 'sample_id':index, 'timestamp_ns':wall, 'monotonic_ns':cutoff,
                       'scheduled_ns':scheduled, 'missed':missed, 'controls':controls}
            self._enqueue('sample', payload, session=session)
            if self.performance is not None:
                left, right = controls.get('left', {}), controls.get('right', {})
                self.performance.emit('record.sample', sample_id=index, record_session_id=session,
                    scheduled_ns=scheduled, cutoff_ns=cutoff, lateness_ns=now-scheduled,
                    capture_ns=cutoff-now, collect_lock_wait_ns=acquired-cutoff,
                    missed=missed, control_arms=len(controls),
                    left_attempt_id=left.get('attempt_id'), right_attempt_id=right.get('attempt_id'),
                    left_published_ns=left.get('monotonic_ns'), right_published_ns=right.get('monotonic_ns'),
                    left_late=left.get('late_observer'), right_late=right.get('late_observer'))
            index += 1

    def shutdown(self, deadline, *, final_entry=None):
        cleanup_margin = min(.5, max(0,deadline-time.monotonic())*.4)
        cancelled = []
        with self._lock:
            if self._manager is None: return False
            with self._admission:
                failed = self._writer_failed
                initiating = not self._closing
                self._closing = True
                if initiating:
                    queued, self._commands = self._commands, deque()
                    sizes, self._command_sizes = self._command_sizes, {}
                    self._command_bytes = 0
                    self._state = 'stopping' if self._state == 'recording' else self._state
                else:
                    queued = ()
            cancelled = [future for _, _, future in queued if future is not None]
            if failed:
                self._flush_gap()
                future = None
            elif initiating:
                self._drain_ingress(all_entries=True)
                self._flush_gap()
                future = Future(); request = uuid.uuid4().hex
                self._shutdown_future = future
                self._futures[request] = future
                self._packet('shutdown', request=request, session=self._session, pending=list(self._pending),
                             final_entry=final_entry, deadline=deadline-cleanup_margin)
            else:
                future = self._shutdown_future
        for queued in cancelled:
            if not queued.done(): queued.set_exception(RuntimeError('recording service is closing'))
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
