"""One aligned action timeline, with an explicit lock and request epoch."""
from __future__ import annotations

from dataclasses import dataclass
import threading
import numpy as np
from rc_policy_sdk.actions import ActionChunk, normalize_actions


@dataclass(frozen=True)
class RequestToken:
    generation: int
    consumed: int


class ActionBuffer:
    def __init__(self):
        self.lock = threading.RLock()
        self.generation = 0
        self.consumed = 0
        self.consumed_since_request = 0
        self.last_trim = 0
        self._actions = np.empty((0, 14), dtype=np.float32)
        self._modes = ()
        self._cursor = 0
        self._submitted = 0
        self._pinned = False

    def install(self, actions) -> None:
        value = normalize_actions(actions)
        with self.lock:
            self._actions = value.actions.copy()
            self._modes = value.modes
            self._cursor = 0
            self._submitted = 0
            self._pinned = False

    def remaining(self) -> np.ndarray:
        with self.lock:
            return self._actions[self._cursor:].copy()

    def current(self) -> np.ndarray | None:
        with self.lock:
            return self._actions[self._submitted].copy() if self._submitted < len(self._actions) else None

    def current_mode(self):
        with self.lock:
            return self._modes[self._submitted] if self._submitted < len(self._modes) else None

    def pin_current(self):
        """A partially submitted slew target cannot be replaced by inference."""
        with self.lock:
            if self._submitted >= len(self._actions): raise RuntimeError('cannot pin empty action buffer')
            self._pinned = True

    def reserve(self):
        """Advance submission only; consumption waits for its publication receipt."""
        with self.lock:
            if self._submitted >= len(self._actions): raise RuntimeError('cannot reserve empty action buffer')
            self._submitted += 1
            self._pinned = False
            return RequestToken(self.generation, self.consumed + self._submitted - self._cursor)

    def confirm(self, token):
        with self.lock:
            if token.generation != self.generation or token.consumed <= self.consumed:
                return False
            if token.consumed != self.consumed + 1 or self._cursor >= self._submitted:
                raise RuntimeError('out-of-order action publication confirmation')
            self._cursor += 1
            self.consumed += 1
            self.consumed_since_request += 1
            return True

    def remaining_payload(self, control_mode="qpos"):
        with self.lock:
            return ActionChunk(self._actions[self._cursor:], self._modes[self._cursor:]).payload(control_mode)

    def consume(self) -> None:
        with self.lock:
            if self._cursor >= len(self._actions):
                raise RuntimeError("不能消费空动作缓存")
            self._cursor += 1
            self._submitted = max(self._submitted, self._cursor)
            self._pinned = False
            self.consumed += 1
            self.consumed_since_request += 1

    def begin_request(self) -> RequestToken:
        with self.lock:
            self.consumed_since_request = 0
            return RequestToken(self.generation, self.consumed)

    def accept(self, request: RequestToken, actions) -> bool:
        value = normalize_actions(actions)
        with self.lock:
            if request.generation != self.generation:
                return False
            protected_end = self._submitted + int(self._pinned)
            protected = protected_end - self._cursor
            self.last_trim = self.consumed - request.consumed + protected
            if self.last_trim >= len(value.actions):
                return False
            self._actions = np.concatenate((self._actions[self._cursor:protected_end], value.actions[self.last_trim:]))
            self._modes = self._modes[self._cursor:protected_end] + value.modes[self.last_trim:]
            self._submitted -= self._cursor
            self._cursor = 0
            return True

    def invalidate(self) -> None:
        with self.lock:
            self.generation += 1
            self._actions = np.empty((0, 14), dtype=np.float32)
            self._modes = ()
            self._cursor = 0
            self.consumed_since_request = 0
            self._submitted = 0
            self._pinned = False
