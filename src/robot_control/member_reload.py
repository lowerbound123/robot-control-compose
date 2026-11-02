"""Replace one independently owned policy deployment while its chain is paused."""
from dataclasses import dataclass
import time

from .config import PolicyRecord, validate_entry
from .operations import ControlRequest
from .process import CandidateRejected
from .runtime_manager import BlockedCleanup, remaining


@dataclass(frozen=True)
class ReloadCandidate:
    record: PolicyRecord
    member: PolicyRecord


class MemberReloadSupport:
    def _start_member_reload(self, record, member, public_id, *, in_place=False):
        self._operation_start('reload_member', old_worker=self._worker, record=record, member=member,
                              epoch=self._control_epoch, commit=True, public_id=public_id, in_place=in_place,
                              source_generation=self.buffer.generation)

    def _queue_reload_retry(self, identity, line, words):
        candidate = self._retry_candidate
        try:
            if len(words) != 2:
                raise ValueError('用法：retry-reload <实例ID>')
            if self.control_owner != 'policy' or self._return_requested:
                raise PermissionError('MCP 接管期间不能重试 policy')
            if self._operation is not None or candidate is None or words[1] != candidate.member.instance_id:
                raise ValueError(f'{words[1]}: 没有可重试的目标实例')
            self._commands.put(ControlRequest(identity, line, 'tui', self._control_epoch,
                               retry_candidate=candidate, retry_generation=self.buffer.generation))
            self._wake.set()
        except (ValueError, PermissionError) as exc:
            self.operations.update(identity, 'failed', error=exc)
            self._emit_command(str(exc), 'error')

    def _dispatch_reload_retry(self, request):
        candidate = request.retry_candidate
        if (self._retry_candidate is not candidate or request.epoch != self._control_epoch or
                request.retry_generation != self.buffer.generation or self.control_owner != 'policy' or
                self._return_requested or self._stop.is_set()):
            self.operations.update(request.operation_id, 'cancelled', error='reload target or control changed')
            return
        self._start_member_reload(candidate.record, candidate.member, request.operation_id)

    def _awaiting_member_retry(self):
        if self._retry_candidate is None:
            return False
        return self._reload_owner is None or (self._worker is self._reload_owner and self._worker.reload_recoverable)

    def _prepare_member_reload(self, job):
        member, record = job['member'], job['record']
        if (self._worker is not job['old_worker'] or job['epoch'] != self._control_epoch
                or job['source_generation'] != self.buffer.generation or self.control_owner != 'policy'
                or self._return_requested or self.registry.get(record.config['id']).version != record.version - 1):
            raise RuntimeError('member reload cancelled: target or control changed')
        chain = job['old_worker'] if record.is_chain else None
        if chain:
            chain.begin_member_reload(member.instance_id)
        self._retry_candidate = ReloadCandidate(record, member)
        self._reload_owner = chain

    def _check_member_job(self, job):
        expected = None if job.get('retired_standalone') else job['old_worker']
        if (self._operation is not job or job['cancel'].is_set() or self._stop.is_set()
                or job['epoch'] != self._control_epoch or self.control_owner != 'policy'
                or self._return_requested or self.buffer.generation != job['prepared_generation']
                or self.registry.get(job['record'].config['id']).version != job['record'].version - 1
                or self._worker is not expected):
            raise RuntimeError('member reload cancelled: control changed')

    def _run_member_reload(self, job):
        if job['in_place']:
            try:
                return self._reinitialize_member(job)
            except Exception as exc:
                if isinstance(exc, CandidateRejected):
                    job['kept_context'] = True
                elif (job.get('prepared') and not job.get('release_started') and job['cancel'].is_set()
                      and not self._stop.is_set()):
                    with self._lock:
                        group = job['old_worker']
                        identity = job['member'].instance_id
                        if job['record'].is_chain and group.reload_recoverable:
                            group.keep_member_context(identity)
                        if group.runnable:
                            self._retry_candidate = self._reload_owner = None
                            job['kept_context'] = True
                raise
        record, member, cancel = job['record'], job['member'], job['cancel']
        identity = member.instance_id
        chain = job['old_worker'] if record.is_chain else None
        validate_entry(member.directory)
        spec = None
        self._finish_reload_cleanup()
        if self.runtime:
            if self.runtime.phase == 'BLOCKED_INFRA' or (
                    self.runtime.phase == 'BLOCKED_CLEANUP' and self._retry_candidate is None):
                raise RuntimeError('runtime is blocked; recovery is required')
            spec = self.runtime.prepare_spec(member, deadline=time.monotonic() + 20, cancel=cancel)
        self._prepare_switch(job)
        deadline = self._cleanup_deadline()
        siblings = {}
        if chain:
            reader_deadline = min(deadline, time.monotonic() + chain.inference_timeout_s)
            chain.wait_other_readers(identity, reader_deadline, cancel)
            siblings = chain.member_workers()
            old = siblings.pop(identity, None)
        else:
            reader_deadline = deadline
            old = job['old_worker']
        with self._external.reader_barrier(siblings, reader_deadline, cancel):
            if old is not None:
                with self._lock:
                    self._check_member_job(job)
                handles = [h for h in self._runtime_groups.get(chain or old, ()) if h.policy_id == identity]
                if self.runtime and (len(handles) != 1 or handles[0] is not getattr(old, 'endpoint', None)):
                    raise BlockedCleanup(f'{identity}: expected the exact container handle of the old worker')
                self._close_worker(old, deadline, handles=handles)
                with self._lock:
                    if chain:
                        chain.member_closed(identity, old)
                        for handle in handles:
                            self._runtime_groups[chain].remove(handle)
                    else:
                        self._worker = self._identity = None
                        job['retired_standalone'] = True
            return self._load_replacement(job, chain, spec)

    def _reinitialize_member(self, job):
        record, member, group, cancel = job['record'], job['member'], job['old_worker'], job['cancel']
        identity = member.instance_id
        members = group.member_workers() if record.is_chain else {identity: group}
        target = members[identity]
        config = member.runtime_config
        # Validate in the loaded module before interrupting an episode or retaining a retry candidate.
        target.validate_reinitialize(config, member.version, cancel)
        self._prepare_switch(job)
        # Only the control thread consumes responses and releases its local frame leases.
        deadline = min(self._cleanup_deadline(), time.monotonic() + group.inference_timeout_s)
        if record.is_chain:
            group.wait_other_readers(None, deadline, cancel)
        while True:
            with self._lock:
                self._check_member_job(job)
                if self._inflight is None and self._observe_inflight is None:
                    break
            if time.monotonic() >= deadline:
                raise TimeoutError(f'{identity}: 等待策略图像释放确认超时')
            cancel.wait(.01)
        deadline = min(self._cleanup_deadline(), time.monotonic() + group.inference_timeout_s)
        siblings = {key: worker for key, worker in members.items() if key != identity}
        with self._external.reader_barrier(siblings, deadline, cancel):
            with self._external.reader_barrier({identity: target}, deadline, cancel):
                with self._lock:
                    self._check_member_job(job)
                try:
                    target.release_context(cancel, stop=self._stop)
                finally:
                    # A pause between the generation check and enqueue leaves the old context intact.
                    job['release_started'] = target.release_dispatched
                cleanup = min(self._cleanup_deadline(), time.monotonic() + target.release_timeout_s)
                self._external.begin_close(target, deadline=cleanup)
                if self._external.close_worker(target, deadline=cleanup):
                    self.exit_code = 1
                    if self.runtime:
                        self.runtime.cleanup_forced = True
            with self._lock:
                self._check_member_job(job)
            self._external.reopen_context(target)
            flags = target.initialize_context(config, member.version, cancel)
            try:
                with self._lock:
                    self._check_member_job(job)
                    if not target.runnable:
                        raise RuntimeError('target exited before reinitialization commit')
                    if record.is_chain:
                        group.finish_reinitialize(member, target, record, flags)
                    else:
                        target.commit_reinitialize(config, member.version, flags)
                    self.registry.install(record)
                    self._retry_candidate = self._reload_owner = None
            except BaseException:
                target.revoke_context()
                raise
        return dict(worker=group, identity=record.config['id'], record=record)

    def _load_replacement(self, job, chain, spec):
        member, record, cancel = job['member'], job['record'], job['cancel']
        identity = member.instance_id
        deadline = time.monotonic() + self.config.get('timeouts', {}).get('initialize_s', 600)
        worker = handle = None
        try:
            with self._lock:
                self._check_member_job(job)
            if self.runtime:
                handle = self.runtime.start(spec, job['id'], cancel, deadline)
                self.runtime.wait_ready(handle, cancel, deadline)
            if chain:
                worker = chain.make_member(member, deadline, handle, self.process_factory)
            else:
                worker = self._make_worker(record, [handle] if handle else [], remaining(deadline))
            with self._owned_lock:
                self._owned.add(worker)
            self._runtime_groups[worker] = [handle] if handle else []
            worker.start(cancel_event=cancel)
            # No lifecycle I/O inside this commit. The control thread serializes
            # cancellation/ownership changes on the same lock.
            with self._lock:
                self._check_member_job(job)
                if not getattr(worker, 'runnable', worker.alive):
                    raise RuntimeError('replacement worker is no longer ready')
                if chain:
                    chain.install_member(identity, worker, record)
                    self._runtime_groups[chain].extend(self._runtime_groups.pop(worker))
                    with self._owned_lock:
                        self._owned.discard(worker)
                self._worker = chain or worker
                self._identity = record.config['id']
                self.registry.install(record)
                self._retry_candidate = self._reload_owner = None
            return dict(worker=chain or worker, identity=record.config['id'], record=record)
        except Exception:
            # start() journals intent before Compose create, even if it cannot
            # return a handle. Reconcile only this attempt, never its siblings.
            if self.runtime and handle is None:
                handle = self.runtime.attempt_handle(identity, job['id'])
            if worker is not None or handle is not None:
                self._pending_reload_cleanup = worker, handle
                self._finish_reload_cleanup()
            raise

    def _finish_reload_cleanup(self):
        """Retry proof for an incomplete candidate before any later deployment."""
        if self._pending_reload_cleanup is None:
            return
        worker, handle = self._pending_reload_cleanup
        deadline = self._cleanup_deadline()
        try:
            if worker is not None:
                self._close_worker(worker, deadline)
            elif self.runtime.stop_and_remove(handle, deadline).forced:
                self.exit_code = 1
        except Exception as exc:
            raise BlockedCleanup(str(exc)) from exc
        self._pending_reload_cleanup = None
