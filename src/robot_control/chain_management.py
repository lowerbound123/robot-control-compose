"""Local editor transactions; model and file work runs on the lifecycle manager."""
import copy
from collections import Counter
from contextlib import contextmanager

from .config import validate_policy
from .config_paths import policy_config_path
from .yaml_config import load_yaml_config
from .operations import ControlRequest


class ChainManagement:
    def submit_policy_chain(self, policies, action='apply', *, expected_control=None):
        if (action not in ('apply', 'save') or not isinstance(policies, list) or not policies
                or any(not isinstance(name, str) for name in policies)):
            raise ValueError('policy_chain requires a nonempty policy list and apply/save action')
        with self._lock:
            self._check_expected_control(expected_control)
            self._require_mcp()
            if self.done.is_set() or self._stop.is_set() or self._operation is not None:
                raise RuntimeError('busy: controller lifecycle or shutdown')
            identity = self.operations.create(f'policy_chain {action}', observer=self._mcp_operation_logger(
                'policy_chain', {'action': action, 'policies': list(policies)}))
            self._commands.put(ControlRequest(identity, f'policy_chain {action}', 'mcp', self._control_epoch,
                chain_action=action, chain_policies=tuple(policies), generation=self.buffer.generation,
                expected_control=copy.deepcopy(expected_control)))
            self._wake.set()
            return identity

    def _check_mcp_job(self, job, *, after_retire=False):
        if job.get('origin') != 'mcp':
            return
        expected = None if after_retire else job['old_worker']
        generation = job.get('prepared_generation', job['source_generation'])
        current = self._worker is expected
        if after_retire and self._worker is job['old_worker']:
            # The lifecycle thread can finish before the control thread consumes
            # switch_empty; the already-confirmed old group is equally retired.
            current = self._worker is None or self._worker in self._confirmed_closed
        if (self._operation is not job or job['cancel'].is_set()
                or not self._policy_control_current('mcp', job['epoch'])
                or self.buffer.generation != generation or not current):
            raise RuntimeError('MCP lifecycle cancelled: control, generation or policy changed')

    def _init_chain_editor(self):
        self._edit_serial = 0
        self._edit_active = False

    def _invalidate_policy_editor(self):
        self._edit_serial += 1
        self._edit_active = False
        if self._operation is not None and self._operation.get('edit_token') is not None:
            self._operation['cancel'].set()

    def manage_policy(self, action, policies=None, *, token=None):
        if action not in ('open', 'apply', 'save', 'close'):
            raise ValueError('unknown policy manage action')
        with self._lock:
            if self.done.is_set() or self._stop.is_set():
                raise RuntimeError('controller is stopping')
            if action == 'open':
                if self.control_owner != 'policy' or self._return_requested:
                    raise PermissionError('policy manage is unavailable during MCP takeover')
                self._invalidate_policy_editor()
                self._edit_active = True
                token = self._edit_serial
                # Urgent pause also cancels a lifecycle already in progress.
                self.submit('pause')
            elif token != self._edit_serial or not self._edit_active:
                raise RuntimeError('stale policy editor; reopen policy manage')
            if action == 'close':
                self._edit_active = False
                return None
            if self.control_owner != 'policy' or self._return_requested:
                raise PermissionError('policy manage is unavailable during MCP takeover')
            identity = self.operations.create(f'policy manage {action}')
            self._commands.put(ControlRequest(identity, f'policy manage {action}', 'tui', self._control_epoch,
                                              chain_action=action, chain_policies=tuple(policies or ()), edit_token=token))
            self._wake.set()
            return identity

    def _dispatch_chain_editor(self, request):
        if request.origin == 'mcp':
            self._require_mcp()
            if request.epoch != self._control_epoch or request.generation != self.buffer.generation:
                raise RuntimeError('MCP chain request is stale')
            self._invalidate_policy_editor()
            if request.chain_action == 'apply':
                self._operation_start('switch', record=None, chain_ids=list(request.chain_policies),
                                      old_worker=self._worker, reset=False)
            else:
                self._operation_start('chain_save', policies=list(request.chain_policies), old_worker=self._worker)
            return
        if request.edit_token != self._edit_serial or not self._edit_active:
            raise RuntimeError('stale policy editor; reopen policy manage')
        if self.control_owner != 'policy' or request.epoch != self._control_epoch:
            raise PermissionError('policy control ownership changed')
        action = request.chain_action
        if action in ('open', 'apply'):
            self._command('pause')
        if action == 'apply':
            self._operation_start('switch', record=None, chain_ids=list(request.chain_policies),
                                  old_worker=self._worker, reset=False, edit_token=request.edit_token)
        else:
            current = self.registry.get(self._identity) if self._identity is not None else None
            self._operation_start('chain_' + action, policies=list(request.chain_policies),
                                  active_order=copy.deepcopy(current.config['policies']) if current and current.is_chain else None,
                                  edit_token=request.edit_token)

    def _run_chain_editor_job(self, job):
        if job.get('origin') == 'mcp':
            @contextmanager
            def guard():
                with self._lock:
                    self._check_mcp_job(job)
                    yield
            with guard():
                pass
            path = self.registry.save_chain(job['policies'], guard=guard)
            return {'editor': {'saved': job['policies'], 'path': str(path)}}
        if job['cancel'].is_set() or job['edit_token'] != self._edit_serial:
            raise RuntimeError('policy editor operation cancelled')
        if job['kind'] == 'chain_save':
            path = self.registry.save_chain(job['policies'])
            return {'editor': {'saved': job['policies'], 'path': str(path), 'token': job['edit_token']}}
        path = policy_config_path(self.registry.directory / 'default_chain')
        saved = []
        if path.exists():
            definition = load_yaml_config(path)
            validate_policy(definition, "default_chain")
            if definition.get('kind') != 'chain' or definition.get('id', 'default_chain') != 'default_chain':
                raise ValueError('default_chain is occupied by an ordinary policy')
            saved = definition['policies']
        catalog = [row for row in self.registry.list() if row.get('kind') != 'chain' and row['id'] != 'default_chain']
        counts = Counter(row['id'] for row in catalog)
        for row in catalog:
            if counts[row['id']] > 1:
                row.update(valid=False, error='duplicate policy ID')
        return {'editor': {'token': job['edit_token'], 'policies': job['active_order'] if job['active_order'] is not None else saved,
                           'applied': job['active_order'], 'saved': saved, 'catalog': catalog}}
