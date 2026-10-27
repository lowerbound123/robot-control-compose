"""Bounded command receipts and independent event cursors for all frontends."""
import copy
from .secret_config import public_value, reject_secret_edit
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass

TERMINAL = {"succeeded", "failed", "cancelled"}


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


class Operations:
    def __init__(self, history=256, max_pending=256):
        self._condition = threading.Condition()
        self._items = OrderedDict()
        self.history, self.max_pending = history, max_pending

    def create(self, command):
        reject_secret_edit(command)
        with self._condition:
            if (command not in ("pause", "return", "exit", "quit")
                    and sum(item["status"] not in TERMINAL for item in self._items.values()) >= self.max_pending):
                raise RuntimeError("command queue is full")
            identity = uuid.uuid4().hex
            self._items[identity] = {"operation_id": identity, "command": command, "status": "queued", "created_at": time.time()}
            self._prune()
            return identity

    def _prune(self):
        terminal = [key for key, value in self._items.items() if value["status"] in TERMINAL]
        for key in terminal[:-self.history]:
            self._items.pop(key)

    def update(self, identity, status, *, result=None, error=None):
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
            self._condition.notify_all()
            self._prune()

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
            for identity in list(self._items):
                if identity not in exclude:
                    self.update(identity, "cancelled", error=reason)
