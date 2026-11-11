"""Stable connection access with generation checks for asynchronous readers."""
import threading


class ConnectorAccess:
    def __init__(self, robot=None):
        self._lock = threading.RLock()
        self.raw = robot
        self.generation = 0
        self.available = robot is not None
        self._observer = None

    def __getattr__(self, name):
        raw = self.raw
        if raw is None:
            raise AttributeError(name)
        return getattr(raw, name)

    @property
    def closed(self):
        return self.raw is None or self.raw.closed

    def suspend(self):
        with self._lock:
            self.available = False
            self.generation += 1

    def bind(self, robot):
        with self._lock:
            self.raw = robot
            self.generation += 1
            self.available = True
            if hasattr(robot, 'set_command_observer'):
                robot.set_command_observer(self._observer)

    def set_command_observer(self, callback):
        with self._lock:
            self._observer = callback
            if self.raw is not None and hasattr(self.raw, 'set_command_observer'):
                self.raw.set_command_observer(callback)

    def _call(self, method, *args, **kwargs):
        with self._lock:
            raw, generation = self.raw, self.generation
            if raw is None or not self.available:
                raise RuntimeError('connector is disconnected or switching')
        result = getattr(raw, method)(*args, **kwargs)
        with self._lock:
            if raw is not self.raw or generation != self.generation or not self.available:
                raise RuntimeError('stale connector generation')
        return result, generation

    def read_state(self):
        value, epoch = self._call('read_state')
        return {**value, 'connector_generation': epoch}

    def read_images(self):
        return self._call('read_images')[0]

    def image_snapshot(self):
        return self._call('image_snapshot')[0]

    def camera_preview_snapshot(self):
        value, epoch = self._call('camera_preview_snapshot')
        return {key: {**frame, 'connector_generation': epoch} for key, frame in value.items()}

    def recording_snapshot(self):
        try:
            value, epoch = self._call('recording_snapshot')
            return {**value, 'connector_generation': epoch,
                    'frames': {key: {**frame, 'connector_generation': epoch} for key, frame in value.get('frames', {}).items()}}
        except RuntimeError as exc:
            return {'state': None, 'state_error': str(exc), 'frames': {}, 'connector_generation': self.generation}

    def feedback_since(self, arm, seq):
        return self._call('feedback_since', arm, seq)[0]

    def execute(self, action, mode='qpos'):
        return self._call('execute', action, **({'mode': mode} if mode != 'qpos' else {}))[0]

    def execute_arms(self, targets):
        return self._call('execute_arms', targets)[0]

    def close(self, *, deadline=None):
        self.suspend()
        if self.raw is not None:
            if getattr(self.raw, 'asynchronous', False):
                self.raw.close(deadline=deadline)
            else:
                self.raw.close()
