"""Bounded command receipts and independent event cursors for all frontends."""
import copy
from .secret_config import public_value, reject_secret_edit
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from contextlib import nullcontext

TERMINAL = {"succeeded", "failed", "cancelled"}


@dataclass(frozen=True)
class PolicySaveTarget:
    identity: str
    record: object = None
    worker: object = None
    version: int | None = None
    generation: int | None = None


@dataclass(frozen=True)
class PolicyConfigEdit:
    instance_id: str
    path: str
    value: object


@dataclass(frozen=True)
class ControlRequest:
    operation_id: str
    line: str
    origin: str
    epoch: int
    steps: tuple = ()
    clear_queue: bool = False
    policy_worker: object = None
    policy_version: int | None = None
    policy_name: str | None = None
    policy_args: tuple = ()
    chain_action: str | None = None
    chain_policies: tuple = ()
    edit_token: int | None = None
    save_target: PolicySaveTarget | None = None
    retry_candidate: object = None
    retry_generation: int | None = None
    generation: int | None = None
    expected_control: dict | None = None
    config_edit: PolicyConfigEdit | None = None
    backend_retry: str | None = None


class Operations:
    def __init__(self, history=256, max_pending=256, *, control_lock=None, control_snapshot=None):
        self.on_event = None
        self._condition = threading.Condition()
        self._items = OrderedDict()
        self._observers = {}
        self.history, self.max_pending = history, max_pending
        self._control_lock, self._control_snapshot = control_lock, control_snapshot

    def create(self, command, *, observer=None):
        reject_secret_edit(command)
        with self._condition:
            if (command not in ("pause", "return", "exit", "quit")
                    and sum(item["status"] not in TERMINAL for item in self._items.values()) >= self.max_pending):
                raise RuntimeError("command queue is full")
            identity = uuid.uuid4().hex
            self._items[identity] = {"operation_id": identity, "command": command, "status": "queued", "created_at": time.time()}
            if observer is not None:
                self._observers[identity] = observer
            self._prune()
            event = dict(self._items[identity])
        self._notify(event, observer)
        return identity

    def _notify(self, item, observer=None):
        # Observability must not affect execution, and callbacks run outside the
        # receipt lock (the controller may already hold its own lifecycle lock).
        for callback in (self.on_event, observer):
            if callback is None:
                continue
            try:
                callback(item)
            except Exception:
                pass

    def _prune(self):
        terminal = [key for key, value in self._items.items() if value["status"] in TERMINAL]
        for key in terminal[:-self.history]:
            self._items.pop(key)

    def update(self, identity, status, *, result=None, error=None, reason=None):
        # Lock order is controller -> receipts, the same as controller dispatch.
        # Capture only the control guard, never a later HTTP reader's live state.
        with self._control_lock if self._control_lock is not None else nullcontext():
            completion = self._control_snapshot() if status in TERMINAL and self._control_snapshot else None
            self._update(identity, status, result=result, error=error, reason=reason, completion=completion)

    def _update(self, identity, status, *, result=None, error=None, reason=None, completion=None):
        if identity is None:
            return
        with self._condition:
            item = self._items.get(identity)
            if item is None or item["status"] in TERMINAL:
                return
            item.update(status=status, updated_at=time.time())
            if result is not None:
                item["result"] = copy.deepcopy(result)
            if error is not None:
                item["error"] = str(error)
            if reason is not None:
                item['reason'] = str(reason)
            if status in TERMINAL:
                if completion is not None:
                    item['completion_control'] = dict(completion)
                # Retain the most recently completed receipts. A long policy
                # episode may have been created before hundreds of short calls.
                self._items.move_to_end(identity)
            self._condition.notify_all()
            event = dict(item)
            observer = (self._observers.pop(identity, None) if status in TERMINAL
                        else self._observers.get(identity))
            self._prune()
        self._notify(event, observer)

    def get(self, identity):
        with self._condition:
            if identity not in self._items:
                raise ValueError("unknown or expired operation_id")
            return public_value(self._items[identity])

    def wait(self, identity, timeout_s):
        if not isinstance(timeout_s, (float, int)) or not 0 <= timeout_s <= 120:
            raise ValueError("timeout_s must be between 0 and 120")
        with self._condition:
            self.get(identity)
            self._condition.wait_for(lambda: identity not in self._items or self._items[identity]["status"] in TERMINAL, timeout_s)
            return self.get(identity)

    def cancel_pending(self, reason, *, exclude=()):
        with self._condition:
            identities = [identity for identity in self._items if identity not in exclude]
        for identity in identities:
            self.update(identity, "cancelled", error=reason)
