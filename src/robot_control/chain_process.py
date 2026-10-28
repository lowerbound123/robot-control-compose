"""One logical policy backed by serial calls to distinct environment workers."""
from collections import deque
import copy
import threading
import time

from rc_policy_sdk.actions import normalize_actions
from rc_policy_sdk.control import control_requests, merge_control_requests

from .chain_config import chain_commands
from .process import PolicyProcess


class _MemberResources:
    def __init__(self, owner, identity):
        self.owner, self.identity = owner, identity

    def handle(self, worker, message):
        if self.owner.resources is None:
            raise RuntimeError("chain has no external resource manager")
        # Every RPC uses the same injective namespace, while the controller owns
        # all registrations under the aggregate for its existing close protocol.
        remote = str(message['remote_id'])
        namespaced = f"{len(self.identity)}:{self.identity}:{remote}"
        return self.owner.resources.handle(self.owner, {**message, 'remote_id': namespaced})


class ChainProcess:
    def __init__(self, record, *, process_factory=PolicyProcess, init_timeout_s=600,
                 inference_timeout_s=60, release_timeout_s=30, endpoints=None, exit_authority=None, on_log=None):
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

    @property
    def alive(self):
        with self._condition:
            # False must mean ALL readers are gone, even during partial startup.
            return any(worker.alive for worker in self._workers.values())

    @property
    def has_readers(self):
        return any(getattr(w, 'has_readers', w.alive) for w in self._workers.values())

    @property
    def cleanup_confirmed(self):
        return all(getattr(w, 'cleanup_confirmed', not w.alive) for w in self._workers.values())

    @property
    def runnable(self):
        return self._ready and not self._failure and all(getattr(w, 'runnable', w.alive) for w in self._workers.values())

    @property
    def pid(self):
        return None  # There is no single process-group identity for a chain.

    @property
    def supports_observe(self):
        with self._condition:
            return any(worker.supports_observe for worker in self._workers.values())

    def monitor_members(self):
        with self._condition:
            return dict(self._workers) if self._ready else {}

    def start(self, cancel_event=None):
        deadline = time.monotonic() + self.init_timeout_s
        with self._lifecycle_lock:
            with self._condition:
                if self._ready or self._workers or self._stop.is_set():
                    raise RuntimeError('cannot start chain more than once')
            try:
                for member in self.record.members:
                    identity = member.config['id']
                    if self._stop.is_set() or (cancel_event and cancel_event.is_set()):
                        raise RuntimeError('chain initialization cancelled')
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f'{identity}: chain initialization timeout')
                    worker = self._factory(member.directory, copy.deepcopy(member.config), member.version,
                                           init_timeout_s=min(remaining, member.config.get('timeouts', {}).get('initialize_s', remaining)),
                                           inference_timeout_s=min(self.inference_timeout_s, member.config.get('timeouts', {}).get('inference_s', self.inference_timeout_s)),
                                           release_timeout_s=min(self.release_timeout_s, member.config.get('timeouts', {}).get('release_s', self.release_timeout_s)), endpoint=self._endpoints.get(identity),
                                           exit_authority=self._exit_authority,
                                           on_log=self._log)
                    worker.resources = _MemberResources(self, identity)
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
        if not self._ready or self._stop.is_set() or self._failure:
            raise RuntimeError(self._failure or 'chain is not ready')

    @staticmethod
    def _identity(request):
        return {key: request[key] for key in ('request_id', 'generation', 'config_version')}

    def _child_request(self, message, worker):
        self._serial += 1
        return {**message, 'request_id': self._serial, 'config_version': worker.config_version}

    def submit(self, request):
        with self._condition:
            self._check_ready()
            if self._request is not None:
                raise RuntimeError('chain inference already in flight')
            if request['config_version'] != self.config_version:
                raise ValueError('chain config version differs')
            chunk = normalize_actions(request['action_chunk'], self.execution['control_mode'], allow_empty=True)
            self._request = {'message': {**request, 'action_chunk': chunk.payload(self.execution['control_mode'])},
                             'index': 0, 'child': None, 'cancel': False, 'control_requests': [],
                             'deadline': time.monotonic() + self.inference_timeout_s}
            self._condition.notify_all()

    def cancel_inference(self):
        with self._condition:
            if self._request is not None:
                self._request['cancel'] = True
            self._condition.notify_all()

    def _finish(self, kind, **extras):
        if kind in ('result', 'cancelled'): extras['images_released'] = True
        self._responses.append({'type': kind, **self._identity(self._request['message']), **extras})
        self._request = None
        self._condition.notify_all()

    def _drive_inference(self):
        request = self._request
        if request is None:
            return
        index = request['index']
        identity = self.config['policies'][index]
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
                child = self._child_request(request['message'], worker)
                worker.submit(child)
                request['child'] = child
            response = worker.poll()
            if response is None:
                return
            if any(response.get(key) != request['child'][key] for key in ('request_id', 'generation', 'config_version')):
                return  # A stale response must not advance the chain.
            if response['type'] != 'result' or response.get('images_released') is not True:
                raise RuntimeError(response.get('error', 'invalid child response'))
            payload = ({'actions': response['actions'], 'modes': response['modes']}
                       if 'modes' in response else response['actions'])
            chunk = normalize_actions(payload, self.execution['control_mode'])
            controls = control_requests(response.get('control_requests', []))
            if controls:
                request['control_requests'] = merge_control_requests(request['control_requests'], [
                    {**controls[0], 'source': {'policy_id': identity, 'cell': index + 1}}])
            if request['cancel']:
                self._finish('cancelled')
            elif index + 1 == len(self.config['policies']):
                extras = {'control_requests': request['control_requests']} if request['control_requests'] else {}
                self._finish('result', **chunk.payload(), **extras)
            else:
                request['index'] += 1
                request['child'] = None
                request['message']['action_chunk'] = chunk.payload(self.execution['control_mode'])
        except Exception as exc:
            self._failure = f'chain cell {index + 1} ({identity}): {exc}'
            # Error is not a lease-release acknowledgement. The controller keeps
            # the frame pinned until close confirms all readers have exited.
            self._finish('error', error=self._failure)

    def _coordinate(self):
        with self._condition:
            while not self._stop.is_set():
                try:
                    for identity, worker in self._workers.items():
                        if not getattr(worker, 'runnable', worker.alive):
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
            if not self._ready or self._stop.is_set() or self._failure or self._observation is not None:
                return False
            pending, errors = {}, []
            for identity, worker in self._workers.items():
                if worker.supports_observe:
                    child = self._child_request(request, worker)
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
            if not self._ready or self._stop.is_set() or self._failure:
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

    @property
    def has_command_results(self):
        return any(getattr(w, 'has_command_results', False) for w in self._workers.values())

    def poll_command(self):
        with self._condition:
            for identity, worker in self._workers.items():
                message = worker.poll_command()
                if message:
                    if message['type'] == 'command_timeout' or message.get('fatal'):
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
