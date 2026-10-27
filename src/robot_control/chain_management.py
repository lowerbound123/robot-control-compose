"""Local editor transactions; model and file work runs on the lifecycle manager."""
import copy
import json
from collections import Counter

from .config import validate_policy
from .operations import ControlRequest


class ChainManagement:
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
            raise ValueError('unknown manage-policy action')
        with self._lock:
            if self.done.is_set() or self._stop.is_set():
                raise RuntimeError('controller is stopping')
            if action == 'open':
                if self.control_owner != 'policy' or self._return_requested:
                    raise PermissionError('manage-policy is unavailable during MCP takeover')
                self._invalidate_policy_editor()
                self._edit_active = True
                token = self._edit_serial
                # Urgent pause also cancels a lifecycle already in progress.
                self.submit('pause')
            elif token != self._edit_serial or not self._edit_active:
                raise RuntimeError('stale policy editor; reopen manage-policy')
            if action == 'close':
                self._edit_active = False
                return None
            if self.control_owner != 'policy' or self._return_requested:
                raise PermissionError('manage-policy is unavailable during MCP takeover')
            identity = self.operations.create(f'manage-policy {action}')
            self._commands.put(ControlRequest(identity, f'manage-policy {action}', 'tui', self._control_epoch,
                                              chain_action=action, chain_policies=tuple(policies or ()), edit_token=token))
            self._wake.set()
            return identity

    def _dispatch_chain_editor(self, request):
        if request.edit_token != self._edit_serial or not self._edit_active:
            raise RuntimeError('stale policy editor; reopen manage-policy')
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
        if job['cancel'].is_set() or job['edit_token'] != self._edit_serial:
            raise RuntimeError('policy editor operation cancelled')
        if job['kind'] == 'chain_save':
            path = self.registry.save_chain(job['policies'])
            return {'editor': {'saved': job['policies'], 'path': str(path), 'token': job['edit_token']}}
        path = self.registry.directory / 'default_chain' / 'policy.json'
        saved = []
        if path.exists():
            definition = json.loads(path.read_text(encoding='utf-8'))
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
