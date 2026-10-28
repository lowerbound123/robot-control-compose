"""Deterministic observations and a controllable external model substitute."""
from collections import deque
import copy
from pathlib import Path
import sys
import threading
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'packages/llm-failure-policy/src'))


def manifest(**params):
    return {'schema_version': 3, 'id': 'llm_failure',
            'execution': {'mode': 'sync', 'call_every_actions': 1, 'control_mode': 'qpos'},
            'hot_reloadable': [], 'params': {
                'task': 'put the block in the tray', 'prompt': 'Check failure using the task and images.',
                'check_every_steps': 60, 'history_size': 5, 'image_max_side': 1024, 'jpeg_quality': 85,
                'provider': {'model': 'deepseek-flash', 'base_url': 'https://api.deepseek.com',
                             'api_key': 'test-key-not-a-real-secret', 'timeout_s': 3.}, **copy.deepcopy(params)}}


def images(value=20):
    return {name: np.full((8, 12, 3), value + offset, np.uint8)
            for name, offset in zip(('head', 'left_wrist', 'right_wrist'), (0, 20, 40))}


def wait(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('llm_failure condition timed out')
        time.sleep(.005)


class Runtime:
    def __init__(self, consumed=0, epoch=1):
        self.consumed_actions, self.control_epoch = consumed, epoch
        self.finishes = []
        self.thread = threading.get_ident()

    def finish(self, message='', *, outcome='completed'):
        assert threading.get_ident() == self.thread, 'background thread called runtime.finish'
        self.finishes.append((message, outcome))


class Provider:
    def __init__(self, *responses):
        self.responses = deque(responses)
        self.calls = []
        self.closed = 0
        self.gates = []

    def decide(self, task, prompt, snapshots):
        self.calls.append((task, prompt, snapshots))
        value = self.responses.popleft() if self.responses else False
        if isinstance(value, tuple):
            gate, value = value
            self.gates.append(gate)
            assert gate.wait(10), 'test model gate was not released'
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed += 1


def make(config=None, provider=None):
    from rc_llm_failure.runtime import FailurePolicy
    return FailurePolicy(config or manifest(), provider=provider or Provider())


def done(instance):
    wait(lambda: instance.pending is not None and instance.pending.done())
