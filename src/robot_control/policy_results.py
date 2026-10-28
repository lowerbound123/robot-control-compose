"""Controller-owned policy episodes, independent of any waiting MCP connection."""
import copy

from .operations import ControlRequest, TERMINAL


class PolicyWaitActive(RuntimeError):
    def __init__(self, operation_id):
        self.operation_id = operation_id
        super().__init__(f'policy wait already active: {operation_id}')


class PolicyResultSupport:
    def _init_policy_results(self):
        self._policy_wait = None
        self._latest_policy_wait = None

    def submit_policy_wait(self):
        """Atomically reserve one episode and revoke manual writes before queuing."""
        with self._lock:
            if self._policy_wait is not None:
                raise PolicyWaitActive(self._policy_wait['operation_id'])
            self._require_mcp()
            if (self.done.is_set() or self._stop.is_set() or not self._connected
                    or self._operation is not None):
                raise RuntimeError('busy: robot connection or lifecycle operation')
            worker = self._worker
            if (worker is None or worker in self._command_faults
                    or not getattr(worker, 'runnable', worker.alive)):
                raise RuntimeError('no runnable policy; switch a policy first')
            identity = self.operations.create('return_and_wait')
            # Existing return callers must still receive a handoff receipt,
            # which completes at resume, independently of the business result.
            handoff = self.operations.create('return')
            self._policy_wait = {'operation_id': identity, 'handoff_operation_id': handoff, 'worker': worker,
                                 'epoch': self._control_epoch, 'generation': self.buffer.generation,
                                 'phase': 'queued'}
            self._latest_policy_wait = identity
            self._return_requested = True
            self._return_operation = handoff
            self._urgent.put(ControlRequest(handoff, 'return_and_wait', 'mcp', self._control_epoch))
            self._wake.set()
            return identity

    def get_policy_result(self, operation_id=None):
        with self._lock:
            identity = self._latest_policy_wait if operation_id is None else operation_id
            if identity is None:
                return {'operation_id': None, 'status': 'none', 'finished': False}
            receipt = self.operations.get(identity)
            if receipt['command'] != 'return_and_wait':
                raise ValueError('operation_id is not a policy wait')
            return {**receipt, 'finished': receipt['status'] in TERMINAL}

    def _begin_policy_wait(self):
        wait = self._policy_wait
        if wait is None or wait['handoff_operation_id'] != self._dispatch_id:
            return
        if (wait['epoch'] != self._control_epoch or wait['worker'] is not self._worker
                or self.control_owner != 'mcp'):
            self._settle_policy_wait('cancelled', reason='人工接管')
            return
        if (self._worker in self._command_faults
                or not getattr(self._worker, 'runnable', self._worker.alive)):
            raise RuntimeError('no runnable policy; switch a policy first')
        self.operations.update(wait['operation_id'], 'running')
        try:
            self._return_control()
        except Exception:
            self._pause(cancel_wait=False)
            raise
        finally:
            # Handoff can change the epoch before its reset barrier fails.
            # Failure still belongs to this episode and must return to MCP.
            wait.update(epoch=self._control_epoch, generation=self.buffer.generation, phase='resuming')

    def _policy_wait_started(self, public_id):
        wait = self._policy_wait
        if wait is None or wait['handoff_operation_id'] != public_id:
            return
        wait.update(epoch=self._control_epoch, generation=self.buffer.generation, phase='running')

    def _matches_policy_wait(self, request):
        wait = self._policy_wait
        return (wait is not None and wait['phase'] == 'running'
                and wait['worker'] is request['worker'] is self._worker
                and wait['epoch'] == self._control_epoch
                and wait['generation'] == request['token'].generation == self.buffer.generation)

    def _settle_policy_wait(self, status, *, result=None, reason=None, reclaim=False):
        """Only the control thread resolves receipts and changes ownership."""
        wait = self._policy_wait
        if wait is None:
            return
        if reclaim and wait['epoch'] == self._control_epoch and wait['worker'] is self._worker:
            if self.control_owner == 'policy':
                self.control_owner = 'mcp'
                self._control_epoch += 1
            # Preserve the confirmed target and all publication fault guards.
            self.direct.targets = copy.deepcopy(self.limiter.commands())
        self._policy_wait = None
        if self._return_operation == wait['handoff_operation_id']:
            self._return_operation = None
            self._return_requested = False
        if status != 'succeeded':
            self.operations.update(wait['handoff_operation_id'], status, error=reason, reason=reason)
        self.operations.update(wait['operation_id'], status, result=result,
                               error=reason if status != 'succeeded' else None, reason=reason)

    def _fail_policy_wait(self, reason):
        self._settle_policy_wait('failed', reason=str(reason), reclaim=True)
