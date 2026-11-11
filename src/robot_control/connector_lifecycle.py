"""Connector transactions. Blocking work belongs to the lifecycle thread."""
import copy
from contextlib import contextmanager
from dataclasses import replace
import queue
import threading
import time

from .connector_access import ConnectorAccess
from .connector_profiles import ConnectorProfiles, profile_name, validate_robot
from .config import PolicyRecord
from .robots import make_robot


class ConnectorLifecycle:
    def _init_connector(self, robot):
        directory = self.config.get('robot_profiles_directory', 'robots')
        self.connector_profiles = ConnectorProfiles(self.root / directory)
        self._initial_robot_config = copy.deepcopy(self.config)
        self._connector_profile = self.config['robot'].get('profile')
        self._connector_target = self._connector_profile
        self._connector_phase = 'created'
        self._connector_error = None
        self._connector_members = {}
        self._connector_failures = {}
        self._connector_pending_cleanup = []
        self._connector_retry_available = False
        self._connector_record_transition = False
        self._connector_record_wanted = False
        self._connector_record_previous = None
        self.robot = ConnectorAccess(robot or (make_robot(self.config, self.root) if 'connector' in self.config['robot'] else None))

    def _connector_connection_config(self, name):
        config = copy.deepcopy(self._initial_robot_config)
        if name is not None:
            profile = self.connector_profiles.read(name)
            identity = profile['connector']
            config['robot'] = {'connector': identity}
            config['connectors'] = {identity: {key: profile[key] for key in ('source', 'config')}}
        return config

    def _bind_connector_runtime(self, robot):
        if robot is not None and getattr(robot, 'asynchronous', False):
            robot.runtime = self.runtime
            robot.wake = self._wake.set
        return robot

    def connector_status(self):
        return dict(profile=self._connector_profile, target_profile=self._connector_target,
                    generation=self.robot.generation, connected=self._connected,
                    phase=self._connector_phase, error=self._connector_error,
                    retry_available=self._connector_retry_available,
                    instances=copy.deepcopy(self._connector_members))

    def connector_properties(self):
        with self._lock:
            config = getattr(self.robot.raw, 'config', None) or self.config['robot']
            return {'robot': copy.deepcopy(config), 'connector': self.connector_status()}

    def _connector_record(self):
        return self.registry.get(self._identity) if self._identity is not None else None

    @staticmethod
    def _connector_workers(record, group):
        if group is None:
            return {}
        return group.member_workers() if record.is_chain else {record.instance_id: group}

    def _connector_command(self, args):
        if args == ['list']:
            self._operation_start('connector_list')
            return
        if args == ['retry']:
            if not self._connector_retry_available:
                raise ValueError('没有可重试的 connector 连接')
            name = self._connector_target
        elif len(args) == 2 and args[0] == 'switch':
            name = profile_name(args[1])
        else:
            raise ValueError('用法：connector list|status|switch <名称>|retry')
        if self._connector_failures or self._retry_candidate is not None:
            raise RuntimeError('请先冷重试失败的 policy 实例或显式 switch 策略组')
        self._operation_start('connector_switch', profile=name, old_worker=self._worker,
                              record=self._connector_record(), in_place=True)

    def _check_connector_job(self, job):
        if (self._operation is not job or job['cancel'].is_set() or self._stop.is_set()
                or self._control_epoch != job['epoch'] or self._return_requested
                or not self._policy_control_current(job['origin'], job['epoch'])
                or self._worker is not job.get('old_worker')):
            raise RuntimeError('connector transaction cancelled: control or policy changed')
        expected = job.get('prepared_generation', job['source_generation'])
        if self.buffer.generation != expected:
            raise RuntimeError('connector transaction cancelled: generation changed')

    def _connector_lifecycle_step(self, job, action, **payload):
        event = dict(type='connector_step', id=job['id'], action=action,
                     complete=threading.Event(), **payload)
        self._results.put(event)
        self._wake.set()
        while not event['complete'].wait(.02):
            with self._lock:
                self._check_connector_job(job)
            self._connector_remaining(job)
        if event.get('error'):
            raise RuntimeError(event['error'])

    def _handle_connector_step(self, result, job):
        try:
            self._check_connector_job(job)
            action = result['action']
            if action == 'prepare':
                self._cancel_direct('connector switch')
                retained = []
                while True:
                    try:
                        request = self._commands.get_nowait()
                    except queue.Empty:
                        break
                    word = request.line.split(maxsplit=1)[0]
                    if request.policy_name is None and word in ('reset', 'r', 'load', 'save', 'resume', 'direct'):
                        self.operations.update(request.operation_id, 'cancelled', error='connector changed before motion dispatch')
                    else:
                        retained.append(request)
                for request in retained:
                    self._commands.put(request)
                self._pause('switching_connector')
                job['prepared'] = True
                job['prepared_generation'] = self.buffer.generation
                if job['record'] is not None and job['record'].is_chain:
                    job['old_worker'].begin_connector_switch()
                self._connector_target = job['profile']
                self._connector_phase = 'draining'
                self._connector_error = None
                self._connector_members = copy.deepcopy(result['members'])
            elif action == 'disconnect':
                self.robot.suspend()
                self._connected = False
                self._hold = self._hold_name = None
                self._published_targets.clear()
                self._connector_phase = 'connecting'
            elif action == 'connect':
                # This is the only publication point for the new sensor epoch.
                self._pool.clear_latest()
                self.robot.bind(result['robot'])
                self._connected = True
                self._connector_profile = job['profile']
                if job['profile'] is not None:
                    self.config['robot'] = {'profile': job['profile']}
                self.limiter.reset(result['state']['qpos'])
                self._hold = self.limiter.previous.copy()
                self._hold_name = None
                self._publish_fault = False
                self._published_targets.clear()
                self._publications.clear()
                self._last_submission = None
                self.direct.targets.clear()
                job['connector_committed'] = True
                self._connector_retry_available = False
                self._connector_phase = 'policies'
            else:
                raise ValueError('unknown connector control step')
        except Exception as exc:
            result['error'] = str(exc)
        finally:
            result['complete'].set()

    def _connector_remaining(self, job):
        deadline = job['deadline']
        if self._shutdown_deadline is not None:
            deadline = min(deadline, self._shutdown_deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('connector transaction deadline exceeded')
        return remaining

    def _connector_wait(self, job, future):
        while not future.done():
            with self._lock:
                self._check_connector_job(job)
            job['cancel'].wait(min(.02, self._connector_remaining(job)))
        return future.result()

    def _connect_initial_connector(self, job):
        try:
            if self.robot.raw is None:
                config = self._connector_connection_config(self._connector_profile)
                self.robot.bind(self._bind_connector_runtime(make_robot(config, self.root)))
            self.robot.raw.connect(cancel_event=job['cancel'])
            self._connector_phase = 'ready'
        except Exception as exc:
            self.robot.suspend()
            self._connector_phase = 'connection_failed'
            self._connector_error = str(exc)
            self._connector_retry_available = True
            raise

    def _close_connector_candidate(self, robot, *, deadline=None):
        if robot not in self._connector_pending_cleanup:
            self._connector_pending_cleanup.append(robot)
        try:
            if getattr(robot, 'asynchronous', False):
                robot.close(deadline=deadline)
            else:
                robot.close()
        finally:
            if getattr(robot, 'cleanup_forced', False):
                self.exit_code = 1
        self._connector_pending_cleanup.remove(robot)

    def _close_connectors(self, *, deadline=None):
        self.robot.suspend()
        robots = list(self._connector_pending_cleanup)
        if self.robot.raw is not None and self.robot.raw not in robots:
            robots.append(self.robot.raw)
        failures = []
        for robot in robots:
            try:
                self._close_connector_candidate(robot, deadline=deadline)
            except Exception as exc:
                failures.append(str(exc))
        if failures:
            raise RuntimeError('; '.join(failures))

    def _run_connector_switch(self, job):
        record, group, cancel = job['record'], job['old_worker'], job['cancel']
        members = record.members if record and record.is_chain else (record,) if record else ()
        workers = self._connector_workers(record, group)
        connection_config = self._connector_connection_config(job['profile'])
        candidate = self._bind_connector_runtime(make_robot(connection_config, self.root))
        # Parse/freeze device inputs and resolve the deployment image before the
        # old connection, policy contexts or recording session are touched.
        candidate.prepare(cancel, deadline=time.monotonic() + self.config.get('timeouts', {}).get('initialize_s', 600))
        config = candidate.config
        job['deadline'] = time.monotonic() + (config.get('connect_timeout_s', 10) +
            config.get('close_timeout_s', 5) + self.config.get('timeouts', {}).get('release_s', 30) * 2 +
            max((w.inference_timeout_s for w in workers.values()), default=1) +
            sum(w.init_timeout_s + w.release_timeout_s + w.inference_timeout_s for w in workers.values()))
        states = {}
        for member in members:
            worker = workers[member.instance_id]
            if not getattr(worker, 'supports_connector_switch', False):
                raise RuntimeError(f'{member.instance_id}: missing connector_switch_v1; upgrade RC/SDK')
            action = member.config['execution'].get('on_connector_switch', 'reinitialize_policy')
            states[member.instance_id] = dict(action=action, status='pending')
            if action == 'reinitialize_policy':
                worker.validate_reinitialize(member.runtime_config, member.version + 1, cancel)
        self._connector_lifecycle_step(job, 'prepare', members=states)
        try:
            if record and record.is_chain:
                group.wait_other_readers(None, time.monotonic() + min(group.inference_timeout_s, self._connector_remaining(job)), cancel)
            deadline = time.monotonic() + min(max((w.inference_timeout_s for w in workers.values()), default=1), self._connector_remaining(job))
            while True:
                with self._lock:
                    self._check_connector_job(job)
                    if self._inflight is None and self._observe_inflight is None:
                        break
                if time.monotonic() >= deadline:
                    raise TimeoutError('等待旧 connector 图像读者确认超时')
                cancel.wait(.01)
            with self._connector_reader_barrier(job, members, workers, deadline):
                # Only the control thread accounts receipts. Preparation stops
                # new submissions, so this is a finite FIFO drain before the
                # recording boundary and before closing either connection.
                while True:
                    with self._lock:
                        self._check_connector_job(job)
                        if not self._publications:
                            break
                    cancel.wait(min(.01, self._connector_remaining(job)))
                self._connector_stop_recording(job)
                self._connector_lifecycle_step(job, 'disconnect')
                close_deadline = min(job['deadline'], time.monotonic() + config.get('close_timeout_s', 5),
                                     self._shutdown_deadline or float('inf'))
                self._close_connectors(deadline=close_deadline)
                self._connector_remaining(job)
                with self._lock:
                    self._check_connector_job(job)
                self._connector_pending_cleanup.append(candidate)
                candidate.connect(cancel_event=cancel, deadline=job['deadline'])
                ready_deadline = min(job['deadline'], time.monotonic() + config.get('connect_timeout_s', 10))
                while True:
                    with self._lock:
                        self._check_connector_job(job)
                    self._connector_remaining(job)
                    try:
                        state = candidate.read_state()
                        images = candidate.read_images()
                        if set(images) != {'head', 'left_wrist', 'right_wrist'}:
                            raise RuntimeError('new connector requires three fresh camera frames')
                        break
                    except RuntimeError:
                        if time.monotonic() >= ready_deadline:
                            raise
                        cancel.wait(.01)
                self._connector_lifecycle_step(job, 'connect', robot=candidate, state=state)
                self._connector_pending_cleanup.remove(candidate)
                candidate = None
                recording_error = None
                try:
                    self._connector_start_recording(job)
                except Exception as exc:
                    recording_error = str(exc)
                for member in members:
                    with self._lock:
                        self._check_connector_job(job)
                    self._connector_remaining(job)
                    worker = workers[member.instance_id]
                    try:
                        if states[member.instance_id]['action'] == 'reset':
                            worker.reset_context(cancel_event=cancel)
                        else:
                            worker.release_context(cancel, stop=self._stop)
                            cleanup = time.monotonic() + min(worker.release_timeout_s, self._connector_remaining(job))
                            self._external.begin_close(worker, deadline=cleanup)
                            if self._external.close_worker(worker, deadline=cleanup):
                                self.exit_code = 1
                            self._external.reopen_context(worker)
                            proposed = replace(member, version=member.version + 1)
                            flags = worker.initialize_context(proposed.runtime_config, proposed.version, cancel)
                            with self._lock:
                                self._check_connector_job(job)
                                if not worker.runnable:
                                    raise RuntimeError('policy exited before connector commit')
                                worker.commit_reinitialize(proposed.runtime_config, proposed.version, flags)
                                self._commit_connector_member(proposed)
                        with self._lock:
                            self._check_connector_job(job)
                            self._connector_members[member.instance_id]['status'] = 'succeeded'
                    except Exception as exc:
                        worker.revoke_context()
                        with self._lock:
                            self._connector_failures[member.instance_id] = member
                            self._connector_members[member.instance_id].update(status='failed', error=str(exc))
                        if cancel.is_set() or self._stop.is_set():
                            raise
                if self._connector_failures or recording_error:
                    raise RuntimeError(recording_error or 'policy connector adaptation failed: ' + ', '.join(self._connector_failures))
            return {}
        finally:
            if candidate is not None:
                self._close_connector_candidate(candidate, deadline=min(job['deadline'],
                    time.monotonic() + config.get('close_timeout_s', 5), self._shutdown_deadline or float('inf')))
            with self._lock:
                self._connector_record_transition = False
                if record and record.is_chain:
                    group.end_connector_switch(set(self._connector_failures))

    @contextmanager
    def _connector_reader_barrier(self, job, members, workers, deadline):
        with self._external.reader_barrier(workers, deadline, job['cancel']):
            try:
                yield
            finally:
                # Revoke unfinished contexts BEFORE reopening external admission.
                # Otherwise an old background task could retain a frame in the gap.
                with self._lock:
                    if job.get('connector_committed'):
                        for member in members:
                            identity = member.instance_id
                            if self._connector_members[identity]['status'] == 'pending':
                                workers[identity].revoke_context()
                                self._connector_failures[identity] = member
                                self._connector_members[identity].update(status='failed', error='connector adaptation interrupted')

    def _commit_connector_member(self, member, replacement=None):
        record = self._connector_record()
        if record.is_chain:
            members = tuple(member if item.instance_id == member.instance_id else item for item in record.members)
            record = replace(record, members=members, version=record.version + 1)
            self._worker.commit_connector_record(record, replacement=replacement)
        else:
            record = member
        self.registry.install(record)
        self._record_configuration()

    def _queue_connector_member_retry(self, operation_id, line, identity, origin, expected_control):
        from .operations import ControlRequest
        try:
            if self._operation is not None or not self._policy_control_current(origin, self._control_epoch):
                raise RuntimeError('busy or control ownership changed')
            if not self._connected:
                raise RuntimeError('connector is not connected')
            self._commands.put(ControlRequest(operation_id, line, origin, self._control_epoch,
                connector_retry=identity, policy_worker=self._worker,
                retry_candidate=self._connector_failures[identity], generation=self.buffer.generation,
                expected_control=copy.deepcopy(expected_control)))
            self._wake.set()
        except Exception as exc:
            self.operations.update(operation_id, 'failed', error=exc)

    def _dispatch_connector_member_retry(self, request):
        if (self._worker is not request.policy_worker or request.generation != self.buffer.generation
                or self._connector_failures.get(request.connector_retry) is not request.retry_candidate
                or not self._policy_control_current(request.origin, request.epoch)):
            raise RuntimeError('connector policy retry target or control changed')
        self._pause('reloading')
        self._operation_start('connector_member_retry', member=request.retry_candidate,
                              old_worker=self._worker, record=self._connector_record(),
                              prepared_generation=self.buffer.generation)

    def _run_connector_member_retry(self, job):
        from .runtime_manager import remaining
        identity = job['member'].instance_id
        record, group, cancel = job['record'], job['old_worker'], job['cancel']
        chain = group if record.is_chain else None
        workers = self._connector_workers(record, group)
        old = workers.get(identity)
        current = next(m for m in record.members if m.instance_id == identity) if chain else record
        member = replace(job['member'], version=max(current.version, old.config_version if old else 0) + 1)
        job['deadline'] = time.monotonic() + self.config.get('timeouts', {}).get('shutdown_s', 115) + self.config.get('timeouts', {}).get('initialize_s', 600)
        spec = self.runtime.prepare_spec(member, deadline=time.monotonic() + min(20, self._connector_remaining(job)), cancel=cancel) if self.runtime else None
        self._finish_reload_cleanup()
        if chain:
            chain.begin_connector_retry()
        worker = handle = None
        try:
            with self._external.reader_barrier(workers, job['deadline'], cancel):
                with self._lock:
                    self._check_connector_job(job)
                handles = [h for h in self._runtime_groups.get(group, ()) if h.policy_id == identity]
                if old is not None:
                    self._close_worker(old, min(job['deadline'], self._cleanup_deadline()), handles=handles)
                    with self._lock:
                        self._check_connector_job(job)
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
                    self._check_connector_job(job)
                    if not worker.runnable:
                        raise RuntimeError('replacement exited before connector retry commit')
                    if chain:
                        self._runtime_groups.setdefault(chain, []).extend(self._runtime_groups.pop(worker))
                        with self._owned_lock:
                            self._owned.discard(worker)
                        self._commit_connector_member(member, replacement=(identity, worker))
                    else:
                        self._worker = worker
                        self._commit_connector_member(member)
                    self._connector_failures.pop(identity)
                    self._connector_members[identity].update(status='succeeded')
                    self._connector_members[identity].pop('error', None)
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
                chain.end_connector_switch(set(self._connector_failures))

    def _finish_connector_job(self, job, result):
        error = result.get('error')
        if job['kind'] == 'connector_list':
            self.operations.update(job.get('public_id'), 'failed' if error else 'succeeded',
                                   result=result.get('profiles'), error=error)
            if error:
                self._emit_command('connector list 失败：' + error, 'error')
            else:
                self._emit_query_result('connector list', result.get('profiles', []))
            return
        if job.get('prepared') or job['kind'] == 'connector_member_retry':
            self._state = 'paused' if self._connected else 'connector_fault'
            self._connector_error = error
            incomplete = job['kind'] == 'connector_switch' and not job.get('connector_committed') and bool(error)
            self._connector_retry_available = (not self._connected or incomplete) and not self._connector_failures
            self._connector_phase = ('policy_failed' if self._connector_failures else
                                   'connection_failed' if not self._connected else 'switch_failed' if incomplete else 'ready')
        self._restore_policy_commands(self._worker)
        self.operations.update(job.get('public_id'), 'cancelled' if job['cancel'].is_set() else 'failed' if error else 'succeeded',
                               result=self.status(), error=error)
        self._emit_command(error or 'connector 切换完成；保持暂停，等待 resume', 'error' if error else 'info')

    def _connector_stop_recording(self, job):
        with self._lock:
            self._check_connector_job(job)
            if not self._connector_record_transition:
                self._connector_record_wanted = self._record_requested
                self._connector_record_previous = self._recorder.status()['session_id']
            self._connector_record_transition = True
            future = self._recorder.command('off')
        self._connector_wait(job, future)

    def _connector_start_recording(self, job):
        with self._lock:
            self._check_connector_job(job)
            if not self._connector_record_wanted:
                return
            self._record_global_version = None
            metadata = self._record_metadata()
            metadata['connector_switch'] = dict(operation_id=job.get('public_id'), previous_session_id=self._connector_record_previous,
                                              profile=self._connector_profile, generation=self.robot.generation)
            future = self._recorder.command('on', metadata)
        self._connector_wait(job, future)
