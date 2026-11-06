"""Backend transactions. Blocking work belongs to the lifecycle thread."""
import copy
from contextlib import contextmanager
from dataclasses import replace
import queue
import threading
import time

from .backend_access import BackendAccess
from .backend_profiles import BackendProfiles, profile_name, validate_robot
from .config import PolicyRecord
from .robots import make_robot


class BackendControl:
    def _init_backend(self, robot):
        directory = self.config.get('robot_profiles_directory', 'robots')
        self.backend_profiles = BackendProfiles(self.root / directory)
        self._initial_robot_config = copy.deepcopy(self.config['robot'])
        self._backend_profile = self.config['robot'].get('profile')
        self._backend_target = self._backend_profile
        self._backend_phase = 'created'
        self._backend_error = None
        self._backend_members = {}
        self._backend_failures = {}
        self._backend_pending_cleanup = []
        self._backend_retry_available = False
        self._backend_record_transition = False
        self._backend_record_wanted = False
        self._backend_record_previous = None
        self.robot = BackendAccess(robot or (make_robot(self.config) if 'backend' in self.config['robot'] else None))

    def backend_status(self):
        return dict(profile=self._backend_profile, target_profile=self._backend_target,
                    generation=self.robot.generation, connected=self._connected,
                    phase=self._backend_phase, error=self._backend_error,
                    retry_available=self._backend_retry_available,
                    instances=copy.deepcopy(self._backend_members))

    def backend_properties(self):
        with self._lock:
            config = getattr(self.robot.raw, 'config', None) or self.config['robot']
            return {'robot': copy.deepcopy(config), 'backend': self.backend_status()}

    def _backend_record(self):
        return self.registry.get(self._identity) if self._identity is not None else None

    @staticmethod
    def _backend_workers(record, group):
        if group is None:
            return {}
        return group.member_workers() if record.is_chain else {record.instance_id: group}

    def _backend_command(self, args):
        if args == ['list']:
            self._operation_start('backend_list')
            return
        if args == ['retry']:
            if not self._backend_retry_available:
                raise ValueError('没有可重试的 backend 连接')
            name = self._backend_target
        elif len(args) == 2 and args[0] == 'switch':
            name = profile_name(args[1])
        else:
            raise ValueError('用法：backend list|status|switch <名称>|retry')
        if self._backend_failures or self._retry_candidate is not None:
            raise RuntimeError('请先冷重试失败的 policy 实例或显式 switch 策略组')
        self._operation_start('backend_switch', profile=name, old_worker=self._worker,
                              record=self._backend_record(), in_place=True)

    def _check_backend_job(self, job):
        if (self._operation is not job or job['cancel'].is_set() or self._stop.is_set()
                or self._control_epoch != job['epoch'] or self._return_requested
                or not self._policy_control_current(job['origin'], job['epoch'])
                or self._worker is not job.get('old_worker')):
            raise RuntimeError('backend transaction cancelled: control or policy changed')
        expected = job.get('prepared_generation', job['source_generation'])
        if self.buffer.generation != expected:
            raise RuntimeError('backend transaction cancelled: generation changed')

    def _backend_control_step(self, job, action, **payload):
        event = dict(type='backend_step', id=job['id'], action=action,
                     complete=threading.Event(), **payload)
        self._results.put(event)
        self._wake.set()
        while not event['complete'].wait(.02):
            with self._lock:
                self._check_backend_job(job)
            self._backend_remaining(job)
        if event.get('error'):
            raise RuntimeError(event['error'])

    def _handle_backend_step(self, result, job):
        try:
            self._check_backend_job(job)
            action = result['action']
            if action == 'prepare':
                self._cancel_direct('backend switch')
                retained = []
                while True:
                    try:
                        request = self._commands.get_nowait()
                    except queue.Empty:
                        break
                    word = request.line.split(maxsplit=1)[0]
                    if request.policy_name is None and word in ('reset', 'r', 'load', 'save', 'resume', 'direct'):
                        self.operations.update(request.operation_id, 'cancelled', error='backend changed before motion dispatch')
                    else:
                        retained.append(request)
                for request in retained:
                    self._commands.put(request)
                self._pause('switching_backend')
                job['prepared'] = True
                job['prepared_generation'] = self.buffer.generation
                if job['record'] is not None and job['record'].is_chain:
                    job['old_worker'].begin_backend_switch()
                self._backend_target = job['profile']
                self._backend_phase = 'draining'
                self._backend_error = None
                self._backend_members = copy.deepcopy(result['members'])
            elif action == 'disconnect':
                self.robot.suspend()
                self._connected = False
                self._hold = self._hold_name = None
                self._published_targets.clear()
                self._backend_phase = 'connecting'
            elif action == 'connect':
                # This is the only publication point for the new sensor epoch.
                self._pool.clear_latest()
                self.robot.bind(result['robot'])
                self._connected = True
                self._backend_profile = job['profile']
                if job['profile'] is not None:
                    self.config['robot'] = {'profile': job['profile']}
                self.limiter.reset(result['state']['qpos'])
                self._hold = self.limiter.previous.copy()
                self._hold_name = None
                self._publish_fault = False
                self._published_targets.clear()
                self.direct.targets.clear()
                job['backend_committed'] = True
                self._backend_retry_available = False
                self._backend_phase = 'policies'
            else:
                raise ValueError('unknown backend control step')
        except Exception as exc:
            result['error'] = str(exc)
        finally:
            result['complete'].set()

    def _backend_remaining(self, job):
        deadline = job['deadline']
        if self._shutdown_deadline is not None:
            deadline = min(deadline, self._shutdown_deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('backend transaction deadline exceeded')
        return remaining

    def _backend_wait(self, job, future):
        while not future.done():
            with self._lock:
                self._check_backend_job(job)
            job['cancel'].wait(min(.02, self._backend_remaining(job)))
        return future.result()

    def _connect_initial_backend(self, job):
        try:
            if self.robot.raw is None:
                config = self.backend_profiles.read(self._backend_profile)
                self.robot.bind(make_robot({'robot': config}))
            self.robot.raw.connect(cancel_event=job['cancel'])
            self._backend_phase = 'ready'
        except Exception as exc:
            self.robot.suspend()
            self._backend_phase = 'connection_failed'
            self._backend_error = str(exc)
            self._backend_retry_available = True
            raise

    def _close_backend_candidate(self, robot):
        if robot not in self._backend_pending_cleanup:
            self._backend_pending_cleanup.append(robot)
        robot.close()
        self._backend_pending_cleanup.remove(robot)

    def _close_backends(self):
        self.robot.suspend()
        robots = list(self._backend_pending_cleanup)
        if self.robot.raw is not None and self.robot.raw not in robots:
            robots.append(self.robot.raw)
        failures = []
        for robot in robots:
            try:
                self._close_backend_candidate(robot)
            except Exception as exc:
                failures.append(str(exc))
        if failures:
            raise RuntimeError('; '.join(failures))

    def _run_backend_switch(self, job):
        record, group, cancel = job['record'], job['old_worker'], job['cancel']
        members = record.members if record and record.is_chain else (record,) if record else ()
        workers = self._backend_workers(record, group)
        config = (self.backend_profiles.read(job['profile']) if job['profile'] is not None
                  else copy.deepcopy(self._initial_robot_config))
        validate_robot(config)
        # Resolve bindings before touching the old connection, but do not initialize ROS.
        if config['backend'] == 'acone':
            from .robots.acone import _load_ros
            _load_ros((config.get('feedback_message_type', config.get('message_type', 'arx5_arm_msg/msg/RobotStatus')),
                       config.get('command_message_type', 'arx5_arm_msg/msg/RobotCmd')))
        job['deadline'] = time.monotonic() + (config.get('connect_timeout_s', 10) +
            config.get('close_timeout_s', 2) + self.config.get('timeouts', {}).get('release_s', 30) * 2 +
            max((w.inference_timeout_s for w in workers.values()), default=1) +
            sum(w.init_timeout_s + w.release_timeout_s + w.inference_timeout_s for w in workers.values()))
        states = {}
        for member in members:
            worker = workers[member.instance_id]
            if not getattr(worker, 'supports_backend_switch', False):
                raise RuntimeError(f'{member.instance_id}: missing backend_switch_v1; upgrade RC/SDK')
            action = member.config['execution'].get('on_backend_switch', 'reinitialize_policy')
            states[member.instance_id] = dict(action=action, status='pending')
            if action == 'reinitialize_policy':
                worker.validate_reinitialize(member.runtime_config, member.version + 1, cancel)
        self._backend_control_step(job, 'prepare', members=states)
        candidate = None
        try:
            if record and record.is_chain:
                group.wait_other_readers(None, time.monotonic() + min(group.inference_timeout_s, self._backend_remaining(job)), cancel)
            deadline = time.monotonic() + min(max((w.inference_timeout_s for w in workers.values()), default=1), self._backend_remaining(job))
            while True:
                with self._lock:
                    self._check_backend_job(job)
                    if self._inflight is None and self._observe_inflight is None:
                        break
                if time.monotonic() >= deadline:
                    raise TimeoutError('等待旧 backend 图像读者确认超时')
                cancel.wait(.01)
            with self._backend_reader_barrier(job, members, workers, deadline):
                self._backend_stop_recording(job)
                self._backend_control_step(job, 'disconnect')
                self._close_backends()
                self._backend_remaining(job)
                with self._lock:
                    self._check_backend_job(job)
                candidate = make_robot({'robot': config})
                self._backend_pending_cleanup.append(candidate)
                candidate.connect(cancel_event=cancel)
                ready_deadline = min(job['deadline'], time.monotonic() + config.get('connect_timeout_s', 10))
                while True:
                    with self._lock:
                        self._check_backend_job(job)
                    self._backend_remaining(job)
                    try:
                        state = candidate.read_state()
                        images = candidate.read_images()
                        if set(images) != {'head', 'left_wrist', 'right_wrist'}:
                            raise RuntimeError('new backend requires three fresh camera frames')
                        break
                    except RuntimeError:
                        if time.monotonic() >= ready_deadline:
                            raise
                        cancel.wait(.01)
                self._backend_control_step(job, 'connect', robot=candidate, state=state)
                self._backend_pending_cleanup.remove(candidate)
                candidate = None
                recording_error = None
                try:
                    self._backend_start_recording(job)
                except Exception as exc:
                    recording_error = str(exc)
                for member in members:
                    with self._lock:
                        self._check_backend_job(job)
                    self._backend_remaining(job)
                    worker = workers[member.instance_id]
                    try:
                        if states[member.instance_id]['action'] == 'reset':
                            worker.reset_context(cancel_event=cancel)
                        else:
                            worker.release_context(cancel, stop=self._stop)
                            cleanup = time.monotonic() + min(worker.release_timeout_s, self._backend_remaining(job))
                            self._external.begin_close(worker, deadline=cleanup)
                            if self._external.close_worker(worker, deadline=cleanup):
                                self.exit_code = 1
                            self._external.reopen_context(worker)
                            proposed = replace(member, version=member.version + 1)
                            flags = worker.initialize_context(proposed.runtime_config, proposed.version, cancel)
                            with self._lock:
                                self._check_backend_job(job)
                                if not worker.runnable:
                                    raise RuntimeError('policy exited before backend commit')
                                worker.commit_reinitialize(proposed.runtime_config, proposed.version, flags)
                                self._commit_backend_member(proposed)
                        with self._lock:
                            self._check_backend_job(job)
                            self._backend_members[member.instance_id]['status'] = 'succeeded'
                    except Exception as exc:
                        worker.revoke_context()
                        with self._lock:
                            self._backend_failures[member.instance_id] = member
                            self._backend_members[member.instance_id].update(status='failed', error=str(exc))
                        if cancel.is_set() or self._stop.is_set():
                            raise
                if self._backend_failures or recording_error:
                    raise RuntimeError(recording_error or 'policy backend adaptation failed: ' + ', '.join(self._backend_failures))
            return {}
        finally:
            if candidate is not None:
                self._close_backend_candidate(candidate)
            with self._lock:
                self._backend_record_transition = False
                if record and record.is_chain:
                    group.end_backend_switch(set(self._backend_failures))

    @contextmanager
    def _backend_reader_barrier(self, job, members, workers, deadline):
        with self._external.reader_barrier(workers, deadline, job['cancel']):
            try:
                yield
            finally:
                # Revoke unfinished contexts BEFORE reopening external admission.
                # Otherwise an old background task could retain a frame in the gap.
                with self._lock:
                    if job.get('backend_committed'):
                        for member in members:
                            identity = member.instance_id
                            if self._backend_members[identity]['status'] == 'pending':
                                workers[identity].revoke_context()
                                self._backend_failures[identity] = member
                                self._backend_members[identity].update(status='failed', error='backend adaptation interrupted')

    def _commit_backend_member(self, member, replacement=None):
        record = self._backend_record()
        if record.is_chain:
            members = tuple(member if item.instance_id == member.instance_id else item for item in record.members)
            record = replace(record, members=members, version=record.version + 1)
            self._worker.commit_backend_record(record, replacement=replacement)
        else:
            record = member
        self.registry.install(record)
        self._record_configuration()

    def _queue_backend_member_retry(self, operation_id, line, identity, origin, expected_control):
        from .operations import ControlRequest
        try:
            if self._operation is not None or not self._policy_control_current(origin, self._control_epoch):
                raise RuntimeError('busy or control ownership changed')
            if not self._connected:
                raise RuntimeError('backend is not connected')
            self._commands.put(ControlRequest(operation_id, line, origin, self._control_epoch,
                backend_retry=identity, policy_worker=self._worker,
                retry_candidate=self._backend_failures[identity], generation=self.buffer.generation,
                expected_control=copy.deepcopy(expected_control)))
            self._wake.set()
        except Exception as exc:
            self.operations.update(operation_id, 'failed', error=exc)

    def _dispatch_backend_member_retry(self, request):
        if (self._worker is not request.policy_worker or request.generation != self.buffer.generation
                or self._backend_failures.get(request.backend_retry) is not request.retry_candidate
                or not self._policy_control_current(request.origin, request.epoch)):
            raise RuntimeError('backend policy retry target or control changed')
        self._pause('reloading')
        self._operation_start('backend_member_retry', member=request.retry_candidate,
                              old_worker=self._worker, record=self._backend_record(),
                              prepared_generation=self.buffer.generation)

    def _run_backend_member_retry(self, job):
        from .runtime_manager import remaining
        identity = job['member'].instance_id
        record, group, cancel = job['record'], job['old_worker'], job['cancel']
        chain = group if record.is_chain else None
        workers = self._backend_workers(record, group)
        old = workers.get(identity)
        current = next(m for m in record.members if m.instance_id == identity) if chain else record
        member = replace(job['member'], version=max(current.version, old.config_version if old else 0) + 1)
        job['deadline'] = time.monotonic() + self.config.get('timeouts', {}).get('shutdown_s', 115) + self.config.get('timeouts', {}).get('initialize_s', 600)
        spec = self.runtime.prepare_spec(member, deadline=time.monotonic() + min(20, self._backend_remaining(job)), cancel=cancel) if self.runtime else None
        self._finish_reload_cleanup()
        if chain:
            chain.begin_backend_retry()
        worker = handle = None
        try:
            with self._external.reader_barrier(workers, job['deadline'], cancel):
                with self._lock:
                    self._check_backend_job(job)
                handles = [h for h in self._runtime_groups.get(group, ()) if h.policy_id == identity]
                if old is not None:
                    self._close_worker(old, min(job['deadline'], self._cleanup_deadline()), handles=handles)
                    with self._lock:
                        self._check_backend_job(job)
                        if chain:
                            chain.member_closed(identity, old)
                            self._runtime_groups[group] = [h for h in self._runtime_groups.get(group, ()) if h not in handles]
                deadline = min(job['deadline'], time.monotonic() + self.config.get('timeouts', {}).get('initialize_s', 600))
                if self.runtime:
                    handle = self.runtime.start(spec, job['id'], cancel, deadline)
                    self.runtime.wait_ready(handle, cancel, deadline)
                worker = (chain.make_member(member, deadline, handle, self.process_factory) if chain
                          else self._make_worker(member, [handle] if handle else [], remaining(deadline)))
                with self._owned_lock:
                    self._owned.add(worker)
                self._runtime_groups[worker] = [handle] if handle else []
                worker.start(cancel_event=cancel)
                with self._lock:
                    self._check_backend_job(job)
                    if not worker.runnable:
                        raise RuntimeError('replacement exited before backend retry commit')
                    if chain:
                        self._runtime_groups.setdefault(chain, []).extend(self._runtime_groups.pop(worker))
                        with self._owned_lock:
                            self._owned.discard(worker)
                        self._commit_backend_member(member, replacement=(identity, worker))
                    else:
                        self._worker = worker
                        self._commit_backend_member(member)
                    self._backend_failures.pop(identity)
                    self._backend_members[identity].update(status='succeeded')
                    self._backend_members[identity].pop('error', None)
            return {}
        except Exception:
            if self.runtime and handle is None:
                handle = self.runtime.attempt_handle(identity, job['id'])
            if worker is not None or handle is not None:
                self._pending_reload_cleanup = worker, handle
                self._finish_reload_cleanup()
            raise
        finally:
            if chain:
                chain.end_backend_switch(set(self._backend_failures))

    def _finish_backend_job(self, job, result):
        error = result.get('error')
        if job['kind'] == 'backend_list':
            self.operations.update(job.get('public_id'), 'failed' if error else 'succeeded',
                                   result=result.get('profiles'), error=error)
            return
        if job.get('prepared') or job['kind'] == 'backend_member_retry':
            self._state = 'paused' if self._connected else 'backend_fault'
            self._backend_error = error
            incomplete = job['kind'] == 'backend_switch' and not job.get('backend_committed') and bool(error)
            self._backend_retry_available = (not self._connected or incomplete) and not self._backend_failures
            self._backend_phase = ('policy_failed' if self._backend_failures else
                                   'connection_failed' if not self._connected else 'switch_failed' if incomplete else 'ready')
        self._restore_policy_commands(self._worker)
        self.operations.update(job.get('public_id'), 'cancelled' if job['cancel'].is_set() else 'failed' if error else 'succeeded',
                               result=self.status(), error=error)
        self._emit_command(error or 'backend 切换完成；保持暂停，等待 resume', 'error' if error else 'info')

    def _backend_stop_recording(self, job):
        with self._lock:
            self._check_backend_job(job)
            if not self._backend_record_transition:
                self._backend_record_wanted = self._record_requested
                self._backend_record_previous = self._recorder.status()['session_id']
            self._backend_record_transition = True
            future = self._recorder.command('off')
        self._backend_wait(job, future)

    def _backend_start_recording(self, job):
        with self._lock:
            self._check_backend_job(job)
            if not self._backend_record_wanted:
                return
            self._record_global_version = None
            metadata = self._record_metadata()
            metadata['backend_switch'] = dict(operation_id=job.get('public_id'), previous_session_id=self._backend_record_previous,
                                              profile=self._backend_profile, generation=self.robot.generation)
            future = self._recorder.command('on', metadata)
        self._backend_wait(job, future)
