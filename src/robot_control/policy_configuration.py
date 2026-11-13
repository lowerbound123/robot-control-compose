"""Policy-owned candidate parsing with controller-owned guarded publication."""
import copy
import time
from dataclasses import replace

from rc_policy_sdk.configuration import same_config

from .config import PolicyRecord, validate_policy
from .member_reload import ReloadCandidate
from .operations import ControlRequest, PolicyConfigEdit
from .process import CandidateRejected


class PolicyConfigurationSupport:
    def _configure_request(self, identity, parsed, origin, expected_control):
        if self._operation is not None or self._retry_candidate is not None:
            raise RuntimeError('busy: policy lifecycle operation')
        if not self._policy_control_current(origin, self._control_epoch):
            raise PermissionError('policy control ownership changed; MCP config edits require take')
        record, member = self._active_member(parsed.target)
        group = self._worker
        target = group.member_workers()[member.instance_id] if record.is_chain else group
        if not getattr(target, 'supports_configure', False):
            raise ValueError(f'{member.instance_id}: policy SDK/module does not support configure')
        if not getattr(group, 'runnable', group.alive):
            raise RuntimeError('policy is not runnable; retry the failed instance first')
        return ControlRequest(identity, 'configure', origin, self._control_epoch,
            policy_worker=group, policy_version=group.config_version, generation=self.buffer.generation,
            config_edit=PolicyConfigEdit(member.instance_id, parsed.key, parsed.value), parsed=parsed,
            expected_control=copy.deepcopy(expected_control))

    def _dispatch_configure(self, request):
        if not self._reload_is_current(request, request.generation):
            self.operations.update(request.operation_id, 'cancelled', error='policy/control state changed before config edit')
            return
        record, member = self._active_member(request.config_edit.instance_id)
        group = request.policy_worker
        target = group.member_workers()[member.instance_id] if record.is_chain else group
        self._operation_start('configure', wait_inflight=True, old_worker=group, worker=group,
            target_worker=target, record=record, member=member, request=request,
            prior_state=self._state, initial_version=record.version, public_id=request.operation_id,
            origin=request.origin)

    def _check_config_preparation(self, job):
        request = job['request']
        if (self._operation is not job or job['cancel'].is_set() or self._stop.is_set()
                or self._worker is not request.policy_worker or self._control_epoch != request.epoch
                or self.buffer.generation != request.generation
                or self._worker.config_version != request.policy_version
                or not self._policy_control_current(request.origin, request.epoch)):
            job['cancel'].set()
            raise RuntimeError('policy/control state changed during config preparation')

    def _configuration_candidate(self, job, candidate):
        old = job['member']
        config = copy.deepcopy(candidate)
        if config.get('id') != old.instance_id:
            raise ValueError('configure changed instance identity')
        config['id'] = old.source_id
        validate_policy(config, old.source_id)
        member = PolicyRecord(old.directory, config, old.version + 1, instance_id=old.instance_id)
        record = job['record']
        if record.is_chain:
            members = tuple(member if m.instance_id == member.instance_id else m for m in record.members)
            if any(m.execution['control_mode'] != members[0].execution['control_mode'] for m in members):
                raise ValueError('control_mode differs from chain head')
            record = PolicyRecord(record.directory, record.config, record.version + 1, members)
        else:
            record = member
        return record, member

    def _wait_config_readers(self, job):
        group, cancel = job['old_worker'], job['cancel']
        deadline = time.monotonic() + getattr(group, 'inference_timeout_s', self.config.get('timeouts', {}).get('inference_s', 60))
        if job['record'].is_chain:
            group.wait_other_readers(None, deadline, cancel)
        while True:
            with self._lock:
                self._check_config_preparation(job)
                if self._inflight is None and self._observe_inflight is None:
                    return deadline
            if time.monotonic() >= deadline:
                raise TimeoutError('等待配置修改前的图像释放确认超时')
            cancel.wait(.01)

    def _run_configure(self, job):
        target, cancel = job['target_worker'], job['cancel']
        baseline_record, baseline_member = job['record'], job['member']
        try:
            self._wait_config_readers(job)
            with self._lock:
                self._check_config_preparation(job)
            edit = job['request'].config_edit
            prepared = target.prepare_config(edit.path, edit.value, cancel)
            before, after = copy.deepcopy(baseline_member.runtime_config), copy.deepcopy(prepared['config'])
            for value in (before, after):
                for field in ('mode', 'call_every_actions'):
                    value.get('execution', {}).pop(field, None)
            scheduling_only = (edit.path in ('execution.mode', 'execution.call_every_actions')
                               and prepared['action'] is None and same_config(before, after))
            with self._lock:
                self._check_config_preparation(job)
                record, member = self._configuration_candidate(job, prepared['config'])
                job.update(record=record, member=member, action=prepared['action'], commit=True,
                           scheduling_only=scheduling_only)
                self._invalidate_policy_editor()
            if prepared['action'] == 'reinitialize':
                # Reuse the same guarded lifecycle job and original public receipt.
                job.update(kind='reload_member', in_place=True)
                return self._run_member_reload(job)
            self._prepare_switch(job)
            group = job['old_worker']
            readers = group.member_workers() if record.is_chain else {member.instance_id: target}
            deadline = time.monotonic() + getattr(group, 'inference_timeout_s', self.config.get('timeouts', {}).get('inference_s', 60))
            with self._external.reader_barrier(readers, deadline, cancel):
                with self._lock:
                    self._check_member_job(job)
                job['apply_started'] = True
                target.apply_config(member.runtime_config, member.version, prepared['action'], cancel)
                with self._lock:
                    self._check_member_job(job)
                target.commit_config(member.runtime_config, member.version, cancel)
                with self._lock:
                    self._check_member_job(job)
                    target.publish_config(member.runtime_config, member.version)
                    if record.is_chain:
                        group.keep_member_context(member.instance_id)
                        group.commit_hot_config(record, member, target)
                    self.registry.install(record)
                    self._retry_candidate = self._reload_owner = None
                    job['committed'] = True
            return {'config': member.config, 'record': record, 'identity': record.config['id']}
        except Exception:
            safe = False
            if not job.get('apply_started') and not job.get('release_started'):
                try:
                    # The cancelled request must still finish its bounded abort.
                    target.abort_config()
                    safe = True
                except Exception:
                    pass
            with self._lock:
                group = job['old_worker']
                if safe:
                    if baseline_record.is_chain and group.reloading_member == baseline_member.instance_id:
                        group.keep_member_context(baseline_member.instance_id)
                    if self._worker is group:
                        self._retry_candidate = self._reload_owner = None
                    job['kept_context'] = True
                else:
                    target.mark_config_failed()
                    # An unconfirmed prepare has no usable candidate: retry the
                    # last committed snapshot with a fresh instance/version.
                    if job['member'].version == baseline_member.version:
                        job['record'], job['member'] = self._configuration_candidate(job, baseline_member.runtime_config)
                    if baseline_record.is_chain and group.reloading_member is None:
                        group.begin_member_reload(baseline_member.instance_id)
                    self._retry_candidate = ReloadCandidate(job['record'], job['member'])
                    self._reload_owner = group if baseline_record.is_chain else None
            raise
