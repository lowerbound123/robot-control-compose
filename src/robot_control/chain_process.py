"""One logical policy backed by serial calls to distinct environment workers."""
from collections import deque
import copy
import threading
import time
import numpy as np

from rc_policy_sdk.actions import ActionChunk, normalize_actions, normalize_result
from rc_policy_sdk.control import control_requests, merge_control_requests

from .chain_config import chain_commands
from .process import PolicyProcess, _emit_inference_trace, _inference_trace_context


class _MemberResources:
    def __init__(self, owner, identity):
        self.owner, self.identity = owner, identity

    def handle(self, worker, message):
        if self.owner.resources is None:
            raise RuntimeError("chain has no external resource manager")
        # The namespace is stable on disk; the live owner is this particular
        # worker, so a late RPC cannot act on its replacement's registration.
        remote = str(message['remote_id'])
        context_id = getattr(worker, 'context_id', None)
        owner = f'{worker.session_id}:{context_id}' if context_id else worker.session_id
        namespaced = f"{len(self.identity)}:{self.identity}:{owner}:{remote}"
        return self.owner.resources.handle(worker, {**message, 'remote_id': namespaced})


class ChainProcess:
    def __init__(self, record, *, process_factory=PolicyProcess, init_timeout_s=600,
                 inference_timeout_s=60, release_timeout_s=30, endpoints=None, exit_authority=None, on_log=None, global_state=None, on_trace=None,
                 on_log_event=None):
        self.record = record
        self.config = copy.deepcopy(record.config)
        self.config_version = record.version
        self.execution = copy.deepcopy(record.execution)
        self.command_specs, self.command_targets = chain_commands(record.members)
        self.resources = None
        self.supports_commands = True
        self.release_error = None
        self.init_timeout_s = init_timeout_s
        self.inference_timeout_s = inference_timeout_s
        self.release_timeout_s = release_timeout_s
        self._factory, self._endpoints, self._log = process_factory, endpoints or {}, on_log
        self._exit_authority = exit_authority
        self.global_state = global_state
        self.on_trace = on_trace
        self.on_log_event = on_log_event
        self._condition = threading.Condition(threading.RLock())
        self._close_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._workers = {}
        self._request = None
        self._observation = None
        self._responses, self._observations = deque(), deque()
        self._serial = 0
        self._stop = threading.Event()
        self._thread = None
        self._ready = False
        self._closed = False
        self._forced = False
        self._failure = None
        self._commands_blocked = False
        self.reloading_member = None
        self._connector_transition = False
        self._connector_blocked = set()
        self._command_members = {}
        self._cancelled_commands = deque()

    @property
    def alive(self):
        with self._condition:
            # False must mean ALL readers are gone, even during partial startup.
            return any(worker.alive for worker in self._workers.values())

    @property
    def has_readers(self):
        with self._condition:
            return any(getattr(w, 'has_readers', w.alive) for w in self._workers.values())

    @property
    def cleanup_confirmed(self):
        with self._condition:
            return all(getattr(w, 'cleanup_confirmed', not w.alive) for w in self._workers.values())

    @property
    def runnable(self):
        with self._condition:
            return (self._ready and self.reloading_member is None and not self._failure and not self._connector_transition and not self._connector_blocked
                    and all(getattr(w, 'runnable', w.alive) for w in self._workers.values()))

    def member_workers(self):
        with self._condition:
            return dict(self._workers)

    def begin_connector_switch(self):
        with self._condition:
            self._check_ready()
            self._connector_transition = True
            self._condition.notify_all()

    def end_connector_switch(self, failed):
        with self._condition:
            self._connector_blocked = set(failed)
            self._connector_transition = False
            self._condition.notify_all()

    def begin_connector_retry(self):
        with self._condition:
            if self._failure or self._stop.is_set() or self._request or self._observation:
                raise RuntimeError(self._failure or 'chain is not ready for connector recovery')
            self._connector_transition = True
            self._condition.notify_all()

    def commit_connector_record(self, record, replacement=None):
        with self._condition:
            if replacement is not None:
                identity, worker = replacement
                self._workers[identity] = worker
                self._workers = {member.instance_id: self._workers[member.instance_id]
                                 for member in record.members if member.instance_id in self._workers}
                self._connector_blocked.discard(identity)
            self.record, self.config_version = record, record.version
            self._condition.notify_all()

    @property
    def reload_recoverable(self):
        with self._condition:
            return self.reloading_member is not None and not self._failure

    @property
    def supports_observe(self):
        with self._condition:
            return any(worker.supports_observe for worker in self._workers.values())

    def monitor_members(self):
        with self._condition:
            return {key: w for key, w in self._workers.items()
                    if key != self.reloading_member} if self._ready else {}

    def make_member(self, member, deadline, endpoint, factory=None):
        identity = member.instance_id
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f'{identity}: chain initialization timeout')
        limits = member.config.get('timeouts', {})
        worker = (factory or self._factory)(member.directory, member.runtime_config, member.version,
            init_timeout_s=min(remaining, limits.get('initialize_s', remaining)),
            inference_timeout_s=min(self.inference_timeout_s, limits.get('inference_s', self.inference_timeout_s)),
            release_timeout_s=min(self.release_timeout_s, limits.get('release_s', self.release_timeout_s)),
            endpoint=endpoint, exit_authority=self._exit_authority, global_state=self.global_state,
            on_log=(lambda message: self._log(f'[{identity}] {message}') if self._log else None),
            **({'on_log_event': self.on_log_event} if self.on_log_event is not None else {}),
            **({'on_trace': self.on_trace} if self.on_trace is not None else {}))
        worker.resources = _MemberResources(self, identity)
        return worker

    def start(self, cancel_event=None):
        deadline = time.monotonic() + self.init_timeout_s
        with self._lifecycle_lock:
            with self._condition:
                if self._ready or self._workers or self._stop.is_set():
                    raise RuntimeError('cannot start chain more than once')
            try:
                for member in self.record.members:
                    identity = member.instance_id
                    if self._stop.is_set() or (cancel_event and cancel_event.is_set()):
                        raise RuntimeError('chain initialization cancelled')
                    worker = self.make_member(member, deadline, self._endpoints.get(identity))
                    with self._condition:
                        if self._stop.is_set():
                            raise RuntimeError('chain initialization cancelled')
                        self._workers[identity] = worker
                    try:
                        worker.start(cancel_event=cancel_event)
                    except Exception as exc:
                        raise RuntimeError(f'chain {identity} initialize: {exc}') from exc
                with self._condition:
                    if self._stop.is_set() or (cancel_event and cancel_event.is_set()):
                        raise RuntimeError('chain initialization cancelled')
                    self._ready = True
                    self._thread = threading.Thread(target=self._coordinate, name='policy-chain', daemon=False)
                    self._thread.start()
            except BaseException:
                if not self._endpoints:
                    self.close()
                raise

    def _check_ready(self):
        if not self._ready or self.reloading_member is not None or self._stop.is_set() or self._failure:
            raise RuntimeError(self._failure or 'chain is not ready')

    def begin_member_reload(self, identity):
        with self._condition:
            if self.reloading_member not in (None, identity) or self._failure:
                raise RuntimeError(self._failure or 'another member is being replaced')
            self.reloading_member = identity
            self.cancel_inference()

    def wait_other_readers(self, identity, deadline, cancel):
        with self._condition:
            while True:
                if self._failure:
                    raise RuntimeError(self._failure)
                readers = self._active_readers()
                readers.discard(identity)
                if not readers:
                    return
                if cancel.is_set():
                    raise RuntimeError('member reload cancelled')
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError('等待图像释放确认超时: ' + ', '.join(sorted(readers)))
                self._condition.wait(min(.05, left))

    def _active_readers(self):
        readers = set(self._observation['pending']) if self._observation else set()
        if self._request and self._request['child'] is not None:
            readers.add(self.record.members[self._request['index']].instance_id)
        return readers

    def member_closed(self, identity, worker):
        """The caller has proved this exact worker and its external readers gone."""
        with self._condition:
            if self._workers.get(identity) is not worker or not worker.cleanup_confirmed:
                raise RuntimeError('missing member exit proof')
            if self._request and self.record.members[self._request['index']].instance_id == identity:
                self._finish('cancelled')
            if self._observation:
                self._observation['pending'].pop(identity, None)
                self._drive_observation()
            for command_id, member in list(self._command_members.items()):
                if member == identity:
                    del self._command_members[command_id]
                    self._cancelled_commands.append(dict(type='command_cancelled', command_id=command_id,
                        member_id=identity, error='policy instance was replaced', config_version=self.config_version))
            del self._workers[identity]
            self._endpoints.pop(identity, None)
            self._condition.notify_all()

    def finish_reinitialize(self, member, worker, record, flags):
        with self._condition:
            if (self.reloading_member != member.instance_id or self._workers.get(member.instance_id) is not worker
                    or self._failure or not all(w.runnable for w in self._workers.values())):
                raise RuntimeError(self._failure or 'stale member reinitialization')
            worker.commit_reinitialize(member.runtime_config, member.version, flags)
            self.record, self.config_version = record, record.version
            self.execution = copy.deepcopy(record.execution)
            self.reloading_member = None
            self._condition.notify_all()

    def keep_member_context(self, identity):
        with self._condition:
            if self.reloading_member != identity:
                raise RuntimeError('member reinitialization changed')
            self.reloading_member = None
            self._condition.notify_all()

    def install_member(self, identity, worker, record):
        with self._condition:
            if (self.reloading_member != identity or identity in self._workers or self._failure
                    or any(not getattr(w, 'runnable', w.alive) for w in self._workers.values())):
                raise RuntimeError(self._failure or 'stale member replacement')
            members = {**self._workers, identity: worker}
            self._workers = {m.instance_id: members[m.instance_id] for m in record.members}
            endpoint = getattr(worker, 'endpoint', None)
            if endpoint is not None:
                self._endpoints[identity] = endpoint
            self.record, self.config_version = record, record.version
            self.execution = copy.deepcopy(record.execution)
            self.reloading_member = None
            self._condition.notify_all()

    def commit_hot_config(self, record, member, worker):
        """Install an acknowledged member patch without replacing any context."""
        with self._condition:
            self._check_ready()
            if (self._request is not None or self._workers.get(member.instance_id) is not worker
                    or record.version != self.config_version + 1 or worker.config_version != member.version):
                raise RuntimeError('stale member hot configuration')
            self.record, self.config_version = record, record.version
            self.execution = copy.deepcopy(record.execution)
            self._condition.notify_all()

    @staticmethod
    def _identity(request):
        return {key: request[key] for key in ('request_id', 'generation', 'config_version')}

    def _child_request(self, message, worker, cell=None):
        self._serial += 1
        runtime = {**message.get('runtime', {}), 'chain_id': self.config['id'],
                   'chain_request_id': message['request_id']}
        if cell is not None:
            runtime['cell'] = cell
        return {**message, 'request_id': self._serial, 'config_version': worker.config_version, 'runtime': runtime}

    def _trace(self, kind, data, *, cell=None, **extras):
        if self._request is not None:
            context = dict(self._request['trace'])
            if cell is not None:
                context['cell'] = cell
            _emit_inference_trace(self.on_trace, self.record.instance_id, self.record.source_id,
                                  kind, context, data, **extras)

    def submit(self, request):
        with self._condition:
            self._check_ready()
            if self._request is not None:
                raise RuntimeError('chain inference already in flight')
            if request['config_version'] != self.config_version:
                raise ValueError('chain config version differs')
            chunk = normalize_actions(request['action_chunk'], self.execution['control_mode'], allow_empty=True)
            context = {**_inference_trace_context(request), 'chain_id': self.config['id'],
                       'chain_request_id': request['request_id']}
            self._request = {'message': {**request, 'action_chunk': chunk.payload(self.execution['control_mode'])},
                             'index': 0, 'child': None, 'cancel': False, 'control_requests': [],
                             'deadline': time.monotonic() + self.inference_timeout_s, 'trace': context}
            self._trace('start', {**self._request['message'], 'type': 'infer'})
            self._condition.notify_all()

    def cancel_inference(self):
        with self._condition:
            if self._request is not None:
                self._request['cancel'] = True
            self._condition.notify_all()

    def _finish(self, kind, *, trace_kind=None, **extras):
        if kind in ('result', 'cancelled'): extras['images_released'] = True
        response = {'type': kind, **self._identity(self._request['message']), **extras}
        self._trace(trace_kind or ('cancel' if kind == 'cancelled' else kind), response, cell=self._request['index'] + 1)
        self._responses.append(response)
        self._request = None
        self._condition.notify_all()

    def _drive_inference(self):
        request = self._request
        if request is None:
            return
        index = request['index']
        identity = self.record.members[index].instance_id
        if self.reloading_member is not None or self._connector_transition:
            child = request['child']
            if child is None:
                self._finish('cancelled')
            else:
                # Do not turn a timeout/EOF into release evidence. Keep the
                # child's identity until its matching ACK or proven retirement.
                response = self._workers[identity].poll_reader_result()
                if (response and response.get('images_released') is True
                        and all(response.get(k) == v for k, v in self._identity(child).items())):
                    self._finish('cancelled')
            return
        worker = self._workers[identity]
        try:
            if self._failure:
                raise RuntimeError(self._failure)
            if time.monotonic() >= request['deadline']:
                raise TimeoutError('whole chain inference timeout')
            if request['child'] is None:
                if request['cancel']:
                    self._finish('cancelled')
                    return
                child = self._child_request(request['message'], worker, cell=index + 1)
                self._trace('stage_start', child, cell=index + 1, child_instance_id=identity)
                worker.submit(child)
                request['child'] = child
            response = worker.poll()
            if response is None:
                return
            if any(response.get(key) != request['child'][key] for key in ('request_id', 'generation', 'config_version')):
                self._trace('stale', response, cell=index + 1, child_instance_id=identity, discarded=True,
                            child_request_id=response.get('request_id'))
                return  # A stale response must not advance the chain.
            self._trace('stage_result' if response['type'] == 'result' else 'stage_error', response,
                        cell=index + 1, child_instance_id=identity, child_request_id=request['child']['request_id'])
            if response['type'] != 'result' or response.get('images_released') is not True:
                raise RuntimeError(response.get('error', 'invalid child response'))
            chunk = normalize_result(response, self.execution['control_mode'])
            previous = request.get('annotated_chunk')
            if chunk.clipped is not None:
                request['annotation_owner'] = {'instance_id': identity, 'context_id': getattr(worker, 'context_id', None)}
            elif previous is not None and previous.clipped is not None:
                markers = np.zeros(chunk.actions.shape, bool)
                for row in range(min(len(chunk.actions), len(previous.actions))):
                    if chunk.modes[row] != previous.modes[row] or not np.array_equal(chunk.actions[row], previous.actions[row]):
                        break
                    markers[row] = previous.clipped[row]
                chunk = ActionChunk(chunk.actions, chunk.modes, markers)
            else:
                request['annotation_owner'] = None
            request['annotated_chunk'] = chunk
            controls = control_requests(response.get('control_requests', []))
            if controls:
                request['control_requests'] = merge_control_requests(request['control_requests'], [
                    {**controls[0], 'source': {'policy_id': identity, 'cell': index + 1}}])
            if request['cancel']:
                self._finish('cancelled')
            elif index + 1 == len(self.config['policies']):
                extras = {'control_requests': request['control_requests']} if request['control_requests'] else {}
                extras.update(chunk.annotations())
                if request.get('annotation_owner') is not None:
                    extras['action_annotation_owner'] = request['annotation_owner']
                self._finish('result', **chunk.payload(), **extras)
            else:
                request['index'] += 1
                request['child'] = None
                request['message']['action_chunk'] = chunk.payload(self.execution['control_mode'])
        except Exception as exc:
            self._failure = f'chain cell {index + 1} ({identity}): {exc}'
            # Error is not a lease-release acknowledgement. The controller keeps
            # the frame pinned until close confirms all readers have exited.
            timed_out = (isinstance(exc, TimeoutError) or time.monotonic() >= request['deadline']
                         or str(exc) in ('policy infer timeout', 'policy inference timeout')
                         or getattr(worker, '_failure', None) in ('policy infer timeout', 'policy inference timeout'))
            self._finish('error', trace_kind='timeout' if timed_out else 'error', error=self._failure)

    def _coordinate(self):
        with self._condition:
            while not self._stop.is_set():
                try:
                    for identity, worker in self._workers.items():
                        if (not self._connector_transition and identity not in self._connector_blocked
                                and identity != self.reloading_member and not getattr(worker, 'runnable', worker.alive)):
                            self._failure = self._failure or f'chain {identity}: worker exited'
                    self._drive_inference()
                    self._drive_observation()
                except Exception as exc:
                    self._failure = f'chain coordinator: {exc}'
                    if self._request is not None:
                        self._finish('error', error=self._failure)
                self._condition.wait(.005)

    def poll(self):
        with self._condition:
            return self._responses.popleft() if self._responses else None

    def observe(self, request):
        with self._condition:
            if not self.runnable or self._stop.is_set() or self._observation is not None:
                return False
            pending, errors = {}, []
            for cell, (identity, worker) in enumerate(self._workers.items(), 1):
                if worker.supports_observe:
                    child = self._child_request(request, worker, cell=cell)
                    try:
                        if not worker.observe(child):
                            raise RuntimeError('observation is busy')
                        pending[identity] = child['request_id']
                    except Exception as exc:
                        errors.append(f'{identity}: {exc}')
            if not pending:
                return False
            self._observation = {'request_id': request['request_id'], 'pending': pending, 'errors': errors}
            self._condition.notify_all()
            return True

    def _drive_observation(self):
        observation = self._observation
        if observation is None:
            return
        for identity, request_id in list(observation['pending'].items()):
            response = self._workers[identity].poll_observation()
            if response and response.get('request_id') == request_id and response.get('images_released') is True:
                del observation['pending'][identity]
                if response['type'] != 'observed':
                    observation['errors'].append(f"{identity}: {response.get('error')}")
        if not observation['pending']:
            errors = observation['errors']
            self._observations.append({'type': 'observation_error' if errors else 'observed',
                                       'request_id': observation['request_id'], 'images_released':True, 'error': '; '.join(errors)})
            self._observation = None
            self._condition.notify_all()

    def poll_observation(self):
        with self._condition:
            return self._observations.popleft() if self._observations else None

    def reset_context(self, cancel_event=None):
        with self._lifecycle_lock:
            self.stop_commands()
            deadline = time.monotonic() + self.inference_timeout_s
            with self._condition:
                self._check_ready()
                while self._request is not None or self._observation is not None:
                    if (cancel_event and cancel_event.is_set()) or self._stop.is_set():
                        raise RuntimeError('chain reset cancelled')
                    if time.monotonic() >= deadline:
                        raise TimeoutError('chain reset barrier timeout')
                    self._condition.wait(.01)
            for identity, worker in self._workers.items():
                try:
                    worker.reset_context(cancel_event=cancel_event)
                except Exception as exc:
                    raise RuntimeError(f'{identity}: reset failed: {exc}') from exc
            self.resume_commands()

    def stop_commands(self):
        with self._condition:
            self._commands_blocked = True
            for worker in self._workers.values():
                worker.stop_commands()

    def resume_commands(self):
        with self._condition:
            if not self.runnable or self._stop.is_set():
                return False
            for worker in self._workers.values():
                worker.resume_commands()
            self._commands_blocked = False
            return True

    def submit_command(self, identity, name, args):
        with self._condition:
            self._check_ready()
            if self._commands_blocked:
                raise RuntimeError('chain commands are blocked')
            member, command = self.command_targets[name]
            self._workers[member].submit_command(identity, command, args)
            self._command_members[identity] = member

    @property
    def has_command_results(self):
        with self._condition:
            return bool(self._cancelled_commands) or any(getattr(w, 'has_command_results', False) for w in self._workers.values())

    def poll_command(self):
        with self._condition:
            if self._cancelled_commands:
                return self._cancelled_commands.popleft()
            for identity, worker in self._workers.items():
                message = worker.poll_command()
                if message:
                    if message['type'] != 'command_started':
                        self._command_members.pop(message['command_id'], None)
                    if message['type'] == 'command_timeout' or message.get('fatal'):
                        if identity == self.reloading_member:
                            return {**message, 'type':'command_cancelled', 'fatal':False, 'member_id':identity}
                        self._failure = f'{identity}: {message.get("error", "command failed")}'
                        message = {**message, 'fatal': True}
                    return {**message, 'member_id': identity, 'config_version': self.config_version}
            return None

    def accept_exit_proof(self, proof):
        worker = self._workers.get(proof.policy_id)
        if worker is not None:
            worker.accept_exit_proof(proof)

    def close(self, *, deadline=None):
        deadline = deadline if deadline is not None else time.monotonic() + self.release_timeout_s + 6
        self._stop.set()
        with self._condition:
            self._ready = False
            if self._request is not None and not self._request.get('trace_cancelled'):
                self._trace('cancel', {**self._identity(self._request['message']), 'type': 'cancelled',
                                       'reason': 'chain closing'}, cell=self._request['index'] + 1)
                self._request['trace_cancelled'] = True
            self._condition.notify_all()
        with self._close_lock:
            if self._closed:
                return self._forced
            errors = []
            groups = {}
            for identity, worker in reversed(list(self._workers.items())):
                key = getattr(worker, 'host', None) or worker
                groups.setdefault(key, []).append((identity, worker))
            from concurrent.futures import ThreadPoolExecutor
            def close_group(group):
                identity, worker = group[0]
                if hasattr(worker, 'close_group'):
                    forced = worker.close_group([w for _, w in group], deadline=deadline)
                else:
                    forced = any([w.close() for _, w in group])
                for name,w in group:
                    if getattr(w, 'has_readers', w.alive):
                        raise RuntimeError(f'{name}: readers remain')
                return forced, next((f'{name}: {w.release_error}' for name,w in group if w.release_error), None)
            if groups:
                executor = ThreadPoolExecutor(max_workers=min(16,len(groups)), thread_name_prefix='chain-release')
                futures = [(group[0][0],executor.submit(close_group,group)) for group in groups.values()]
                try:
                    for identity,future in futures:
                        try:
                            forced,error = future.result(timeout=max(0,deadline-time.monotonic()))
                            self._forced |= forced
                            self.release_error = error or self.release_error
                        except Exception as exc:
                            errors.append(f'{identity}: {exc}')
                finally:
                    executor.shutdown(wait=False,cancel_futures=True)
            if self._thread is not None and self._thread is not threading.current_thread():
                self._thread.join(1)
                if self._thread.is_alive():
                    errors.append('chain coordinator did not exit')
            if errors:
                self.release_error = '; '.join(errors)
                raise RuntimeError(self.release_error)
            self._closed = True
            return self._forced
