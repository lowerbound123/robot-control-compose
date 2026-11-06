"""Display-only 50 Hz feedback snapshots; one bounded producer for all readers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import math
import threading
import time

FPS = 50


@dataclass(frozen=True)
class StateFrame:
    sequence: int
    sampled_monotonic: float
    data: bytes


class StateSubscription:
    def __init__(self, owner):
        self.owner = owner
        self.loop = asyncio.get_running_loop()
        self.event = asyncio.Event()
        self.notified = self.closed = False
        self.last_sequence = 0

    async def next_frame(self):
        while not self.closed:
            with self.owner.condition:
                self.event.clear()
                self.notified = False
                if self.owner.closed or self.owner.stopping():
                    self.close()
                    return None
                frame = self.owner.frame
                if frame is not None and frame.sequence != self.last_sequence:
                    self.last_sequence = frame.sequence
                    return frame
            try:
                await asyncio.wait_for(self.event.wait(), .25)
            except asyncio.TimeoutError:
                pass
        return None

    def close(self):
        with self.owner.condition:
            if self.closed:
                return
            self.closed = True
            self.owner.readers.discard(self)
            if not self.owner.readers:
                self.owner.generation += 1
                self.owner.frame = None
            self.owner.condition.notify_all()


class WebState:
    def __init__(self, source, *, run_id, stopping=lambda: False, on_failure=lambda exc: None):
        self.source, self.run_id, self.stopping = source, run_id, stopping
        self.on_failure = on_failure
        self.condition = threading.Condition(threading.RLock())
        self.readers = set()
        self.frame = self.thread = None
        self.closed = False
        self.sequence = self.generation = 0

    @property
    def subscriber_count(self):
        with self.condition:
            return len(self.readers)

    def subscribe(self):
        reader = StateSubscription(self)
        with self.condition:
            if self.closed or self.stopping():
                raise RuntimeError('state stream is stopping')
            self.readers.add(reader)
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name='rc-web-state', daemon=True)
                self.thread.start()
            self.condition.notify_all()
        return reader

    def _wake(self):
        for reader in tuple(self.readers):
            if not reader.notified:
                reader.notified = True
                try:
                    reader.loop.call_soon_threadsafe(reader.event.set)
                except RuntimeError:
                    reader.close()

    def _run(self):
        try:
            self._sample()
        except BaseException as exc:
            self.stop()
            self.on_failure(exc)

    def _sample(self):
        next_due = 0.
        while True:
            with self.condition:
                while not self.readers and not self.closed and not self.stopping():
                    next_due = 0.
                    self.condition.wait(.25)
                if self.closed or self.stopping():
                    return
                delay = next_due - time.monotonic()
                if delay > 0:
                    self.condition.wait(min(delay, .1))
                    continue
                generation = self.generation
            sampled = time.monotonic()
            state, error = None, None
            try:
                observation = self.source()
                state = {key: observation[key] for key in ('qpos', 'eef', 'sequences')}
                # Mock feedback has one acquisition timestamp; hardware has one per arm.
                state['timestamps'] = (observation['timestamps'] if 'timestamps' in observation else
                                       {arm: observation['timestamp'] for arm in ('left', 'right')})
                state['feedback_age_s'] = (observation['feedback_age_s'] if 'feedback_age_s' in observation else
                                          {arm: max(0., time.time() - stamp) for arm, stamp in state['timestamps'].items()})
                for key in ('qpos', 'eef'):
                    if len(state[key]) != 14 or not all(math.isfinite(float(value)) for value in state[key]):
                        raise ValueError(f'{key} requires 14 finite values')
                # Validate while still inside the recoverable source-error boundary.
                json.dumps(state, allow_nan=False)
            except Exception as exc:
                state, error = None, str(exc)[:512]
            with self.condition:
                if self.closed or self.stopping():
                    return
                if generation == self.generation and self.readers:
                    self.sequence += 1
                    payload = dict(run_id=self.run_id, sequence=self.sequence,
                                   sampled_monotonic=sampled, state=state, error=error)
                    encoded = ('data: ' + json.dumps(payload, allow_nan=False, separators=(',', ':')) + '\n\n').encode()
                    self.frame = StateFrame(self.sequence, sampled, encoded)
                    self._wake()
            # Absolute pacing avoids adding source/serialization duration to each period.
            next_due = next_due + 1 / FPS if next_due else sampled + 1 / FPS
            now = time.monotonic()
            if next_due < now - 1 / FPS:
                next_due = now

    def stop(self):
        with self.condition:
            self.closed = True
            self.frame = None
            self._wake()
            self.condition.notify_all()

    def close(self, deadline):
        self.stop()
        if self.thread is not None:
            self.thread.join(max(0., deadline - time.monotonic()))
        return self.thread is None or not self.thread.is_alive()


async def state_events(reader):
    try:
        while (frame := await reader.next_frame()) is not None:
            yield frame.data
    finally:
        reader.close()
