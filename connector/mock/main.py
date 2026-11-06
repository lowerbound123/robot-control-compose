"""Deterministic feedback and synthetic RGB cameras for controller testing."""
import copy
import threading
import time
from collections import deque

import numpy as np
from rc_connector_sdk.actions import arm_targets, validate_arm_targets


def validate_qpos(action):
    target = np.asarray(action, dtype=np.float64)
    if target.shape != (14,) or not np.all(np.isfinite(target)):
        raise ValueError("qpos action must contain exactly 14 finite values")
    return target.copy()


class MockRobot:
    def __init__(self, config=None):
        self.config = dict(config or {})
        self._feedback_hz = self.config.get('feedback_hz', 100)
        if type(self._feedback_hz) not in (int, float) or not np.isfinite(self._feedback_hz) or self._feedback_hz <= 0:
            raise ValueError('feedback_hz must be finite and positive')
        self.commands = []
        self.command_modes = []
        self.closed = False
        self._connected = False
        self._lock = threading.Lock()
        self._qpos = validate_qpos(self.config.get("initial_qpos", [0] * 6 + [-5] + [0] * 6 + [-5]))
        self._epos = validate_qpos(self.config.get("initial_epos", self._qpos))
        self._seq = 0
        self._history = {arm: deque(maxlen=512) for arm in ("left", "right")}
        self._images = {}
        self._camera_epoch = None
        self._camera_wall_epoch = None
        self._recording_state = None
        self._command_observer = None
        self._sample_stop = threading.Event()
        self._sample_thread = None
        self._feedback_received = 0.
        self._feedback_timestamp = 0.
        self.camera_topics = {name: name for name in ('head', 'left_wrist', 'right_wrist')}

    def connect(self, cancel_event=None):
        with self._lock:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("robot connection cancelled")
            if self.closed:
                raise RuntimeError("robot is closed")
            if self._connected:
                return
            height, width = self.config.get("image_size", (64, 96))
            for index, camera in enumerate(("head", "left_wrist", "right_wrist")):
                image = np.zeros((height, width, 3), dtype=np.uint8)
                image[..., index] = 180
                image[..., (index + 1) % 3] = np.arange(width, dtype=np.uint8)
                image.flags.writeable = False
                self._images[camera] = image
            self._camera_epoch = time.monotonic()
            self._camera_wall_epoch = time.time()
            self._connected = True
            self._sample_locked()
            self._sample_thread = threading.Thread(target=self._sample_loop, name='mock-feedback', daemon=True)
            self._sample_thread.start()

    def _sample_locked(self):
        self._seq += 1
        self._feedback_received, self._feedback_timestamp = time.monotonic(), time.time()
        for i, arm in enumerate(('left', 'right')):
            self._history[arm].append({'seq': self._seq, 'received': self._feedback_received,
                'qpos': self._qpos[i*7:i*7+7].tolist(), 'epos': self._epos[i*7:i*7+7].tolist()})
        self._recording_state = self._state_locked()

    def _sample_loop(self):
        while not self._sample_stop.wait(1 / self._feedback_hz):
            with self._lock:
                if self.closed: return
                self._sample_locked()

    def _check_ready(self):
        if self.closed:
            raise RuntimeError("robot is closed")
        if not self._connected:
            raise RuntimeError("robot must connect before use")

    def read_state(self):
        with self._lock:
            self._check_ready()
            return self._state_locked()

    def _state_locked(self):
        return {
            "qpos": self._qpos.tolist(), "qvel": [0.0] * 14,
            "effort": [0.0] * 14, "eef": self._epos.tolist(),
            "timestamp": self._feedback_timestamp,
            "feedback_age_s": {arm: time.monotonic() - self._feedback_received for arm in self._history},
            "sequences": {arm: self._seq for arm in self._history},
        }

    def _camera_snapshot_locked(self):
        if self._camera_epoch is None:
            return {}
        # A camera arrival has one identity across control, preview and recording
        # reads; polling cannot manufacture newer sensor timestamps.
        tick = max(0, int((time.monotonic() - self._camera_epoch) * 30))
        return {name: {"image": image, "sequence": tick + 1,
                       "timestamp": self._camera_wall_epoch + tick / 30,
                       "received": self._camera_epoch + tick / 30}
                for name, image in self._images.items()}

    def read_images(self):
        with self._lock:
            self._check_ready()
            return {name: (frame["image"], frame["timestamp"])
                    for name, frame in self._camera_snapshot_locked().items()}

    def image_snapshot(self):
        return self.read_images(), {}

    def camera_preview_snapshot(self):
        with self._lock:
            self._check_ready()
            return self._camera_snapshot_locked()

    def recording_snapshot(self):
        """Read cached feedback and independent synthetic camera arrivals."""
        with self._lock:
            error = "robot is closed" if self.closed else (None if self._connected else "robot must connect before use")
            return {"state": copy.deepcopy(self._recording_state), "state_error": error,
                    "frames": {name: {**frame, "valid": error is None, "error": error}
                               for name, frame in self._camera_snapshot_locked().items()}}

    def set_command_observer(self, callback):
        self._command_observer = callback

    def _observe_command(self, arm, target):
        if self._command_observer is not None:
            try:
                self._command_observer({"arm": arm, "mode": target["mode"],
                                        "values7": list(target["values"]),
                                        "timestamp_ns": time.time_ns(), "monotonic_ns": time.monotonic_ns()})
            except Exception:
                pass

    def feedback_since(self, arm, seq):
        with self._lock:
            return [dict(sample) for sample in self._history[arm] if sample["seq"] > seq]

    def execute(self, action, mode="qpos"):
        self.execute_arms(arm_targets(validate_qpos(action), mode))

    def execute_arms(self, targets):
        targets = validate_arm_targets(targets)
        with self._lock:
            self._check_ready()
            command = self._qpos.copy()
            for arm, target in targets.items():
                start = 0 if arm == "left" else 7
                values = target["values"]
                (self._qpos if target["mode"] == "qpos" else self._epos)[start:start+7] = values
                self._qpos[start+6] = self._epos[start+6] = values[6]
                command[start:start+7] = values
                self._observe_command(arm, target)
            self.commands.append(command)
            self.command_modes.append({arm: target["mode"] for arm, target in targets.items()})
            # Inspection history must not grow for the lifetime of the service.
            del self.commands[:-4096]
            del self.command_modes[:-4096]
            self._sample_locked()

    def close(self):
        self._sample_stop.set()
        with self._lock:
            self.closed = True
            self._connected = False
            self._images.clear()
        if self._sample_thread is not None: self._sample_thread.join(1)


def create_connector(config):
    return MockRobot(config)
