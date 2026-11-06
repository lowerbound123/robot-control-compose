"""Human text batches; every method runs under the controller lock.

The control loop advances one child at a time. HTTP callers only enqueue/query;
they never own the lifetime of an accepted batch.
"""
from collections import deque
from dataclasses import dataclass
import time

from rc_policy_sdk.command_syntax import parse_command
from .operations import TERMINAL
from .secret_config import reject_secret_edit


def parse_batch(text):
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError('text must contain 1-4000 characters')
    lines = [(number, line.strip()) for number, line in enumerate(text.replace('\r\n', '\n').split('\n'), 1)
             if line.strip()]
    if len(lines) > 64:
        raise ValueError('at most 64 commands per batch')
    for index, (_, line) in enumerate(lines):
        reject_secret_edit(line)
        if any(char in line for char in ('\x00', '\r')):
            raise ValueError('invalid command line')
        parsed = parse_command(line)
        if parsed.kind in ('mcp_tool', 'mcp_help'):
            raise ValueError('recursive MCP commands are not supported')
        if parsed.local_only:
            raise ValueError('local-only terminal command is not supported')
        if parsed.kind == 'exit' and index != len(lines) - 1:
            raise ValueError('exit must be the final command')
    return lines


@dataclass
class Batch:
    identity: str
    steps: list
    index: int = 0
    child: str | None = None
    started: bool = False
    cancel_reason: str | None = None


class TerminalBatches:
    def __init__(self, controller):
        self.rc = controller
        self.batches = {}
        self.queue = deque()
        self.active = None
        # Track queued children until dispatch or terminal collection. The
        # controller also refuses terminal receipts still left in its queue.
        self.queued_children = {}

    def submit(self, text, expected_control=None):
        lines = parse_batch(text)  # Validate all lines before any log/receipt.
        self.rc._check_expected_control(expected_control)
        if self.rc._stop.is_set() or self.rc.done.is_set():
            raise RuntimeError('controller is stopping')
        if len(self.batches) >= 32:
            raise RuntimeError('text batch queue is full')
        identity = self.rc.operations.create('tui_command', observer=self.rc._mcp_operation_logger(
            'tui_command', {'lines': len(lines)}))
        batch = Batch(identity, [{'line': number, 'command': line, 'status': 'queued'} for number, line in lines])
        self.batches[identity] = batch
        self.queue.append(identity)
        self.rc._wake.set()
        return identity

    def _result(self, batch):
        return {'kind': 'tui_batch', 'total': len(batch.steps),
                'completed': sum(row['status'] in TERMINAL for row in batch.steps), 'steps': batch.steps}

    def _publish(self, batch):
        self.rc.operations.update(batch.identity, 'running', result=self._result(batch), reason=batch.cancel_reason)

    def _finish(self, batch, status, error=None):
        for row in batch.steps:
            if row['status'] not in TERMINAL:
                row.update(status='cancelled', reason=batch.cancel_reason or 'previous command did not succeed')
        self.rc.operations.update(batch.identity, status, result=self._result(batch),
                                  error=error, reason=batch.cancel_reason)
        self.batches.pop(batch.identity, None)
        if self.active == batch.identity:
            self.active = None
        if batch.identity in self.queue:
            self.queue.remove(batch.identity)

    def cancel(self, identity, reason='cancelled by caller'):
        receipt = self.rc.get_operation(identity)
        if receipt['command'] != 'tui_command':
            raise ValueError('operation is not a text batch')
        batch = self.batches.get(identity)
        if batch is None:
            return receipt
        batch.cancel_reason = reason
        if batch.child is not None and not batch.started:
            self.queued_children[batch.child] = None
            self.rc._policy_reloads.pop(batch.child, None)
            if self.rc._return_operation == batch.child:
                self.rc._return_operation = None
                self.rc._return_requested = False
            self.rc.operations.update(batch.child, 'cancelled', reason=reason)
            self._collect(batch)
        if batch.child is None:
            self._finish(batch, 'cancelled')
        else:
            for row in batch.steps[batch.index + 1:]:
                row.update(status='cancelled', reason=reason)
            self._publish(batch)
        self.rc._wake.set()
        return self.rc.get_operation(identity)

    def interrupt(self):
        for identity in tuple(self.batches):
            self.cancel(identity, 'interrupted by local TUI input')

    def claim_child(self, identity):
        if identity not in self.queued_children:
            return True
        parent = self.queued_children.pop(identity)
        batch = self.batches.get(parent)
        if batch is None or (batch.cancel_reason and not batch.started):
            return False
        batch.started = True
        batch.steps[batch.index]['started_at'] = time.time()
        return True

    def _collect(self, batch):
        row = batch.steps[batch.index]
        receipt = self.rc.get_operation(batch.child)
        changed = row['status'] != receipt['status']
        row['status'] = receipt['status']
        if receipt['status'] not in TERMINAL:
            if changed:
                self._publish(batch)
            return False
        row.update({key: receipt[key] for key in ('result', 'error', 'reason') if key in receipt})
        self.queued_children.pop(batch.child, None)
        batch.child = None
        batch.index += 1
        if batch.cancel_reason:
            self._finish(batch, 'cancelled')
        elif receipt['status'] != 'succeeded':
            self._finish(batch, receipt['status'], receipt.get('error') or receipt.get('reason'))
        elif batch.index == len(batch.steps):
            self._finish(batch, 'succeeded')
        else:
            self._publish(batch)
        return True

    def poll(self):
        if self.rc._stop.is_set():
            return  # Shutdown settles all children before their parent receipts.
        if self.active is None:
            if not self.queue:
                return
            self.active = self.queue.popleft()
            self._publish(self.batches[self.active])
        batch = self.batches[self.active]
        if batch.child is not None and not self._collect(batch):
            return
        if batch.identity not in self.batches:
            return
        row = batch.steps[batch.index]
        try:
            started_at = time.time()
            child = self.rc.submit(row['command'], origin='tui', batch_id=batch.identity)
            batch.child = child
            row['operation_id'] = child
            receipt = self.rc.get_operation(child)
            # Queries/recording run at admission. Exit sets _stop at admission.
            batch.started = receipt['status'] != 'queued' or row['command'] in ('exit', 'quit')
            if batch.started:
                row['started_at'] = started_at
            if receipt['status'] == 'queued':
                self.queued_children[child] = batch.identity
            row['status'] = receipt['status']
            self._publish(batch)
        except (ValueError, RuntimeError, PermissionError) as exc:
            row.update(status='failed', error=str(exc))
            self._finish(batch, 'failed', str(exc))

    def finish_shutdown(self):
        for batch in tuple(self.batches.values()):
            own_exit = batch.child is not None and batch.steps[batch.index]['command'] in ('exit', 'quit')
            if not own_exit:
                batch.cancel_reason = batch.cancel_reason or 'controller stopped'
            if batch.child is not None:
                self._collect(batch)
            if batch.identity in self.batches:
                self._finish(batch, 'cancelled')
        self.queued_children.clear()
