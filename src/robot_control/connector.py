"""RC-side device proxy. Sensor snapshots are local; only RC submits motion."""
from collections import deque
import copy
import json
import math
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np

from rc_connector_sdk.actions import arm_targets
from rc_connector_sdk.client import ConnectorClient
from rc_connector_sdk.images import map_images
from .yaml_config import load_yaml_config
from .safety_config import safety_settings


class ImagesPending(RuntimeError):
    """A demand arrived after idle; the RC sampler is obtaining a new snapshot."""


class ConnectorRobot:
    asynchronous = True

    def __init__(self, config, root):
        self.rc_config, self.root = config, Path(root)
        self.instance_id = config['robot']['connector']
        self.spec = config['connectors'][self.instance_id]
        self.directory = (self.root / config['connector_directory'] / self.spec['source']).resolve()
        self.config_path = self.directory / self.spec['config']
        self.config = None
        self.safety = None
        self.runtime = self.handle = self.client = self.process = None
        self.closed = False
        self._prepared_spec = None
        self._prepared = False
        self._client_close_confirmed = False
        self._client_close_error = None
        self.cleanup_forced = False
        self._lock = threading.RLock()
        self._result_lock = threading.RLock()
        self._stop = threading.Event()
        self._sampler = None
        self._temporary = None
        self._state = None
        self._state_error = 'connector has not connected'
        self._state_received = 0.
        self._frames = {}
        self._images_checked_at = 0.
        self._images_generation = 0
        self._required_image_generation = None
        self._feedback_errors = {}
        self._history = {arm: deque(maxlen=512) for arm in ('left', 'right')}
        self._image_requested = True
        self._command_observer = None
        self._receipts = {}
        self._sent = {}
        self._attempts = {}
        self.last_submission = None
        self.wake = lambda: None
        # Bounded inspection history, also used by recording/integration tests.
        self._commands, self._command_modes = deque(maxlen=4096), deque(maxlen=4096)

    @property
    def commands(self):
        with self._result_lock: return tuple(command.copy() for command in self._commands)

    @property
    def command_modes(self):
        with self._result_lock: return tuple(dict(modes) for modes in self._command_modes)

    def prepare(self, cancel_event=None, *, deadline=None):
        """Freeze connection inputs without starting a device or loading ROS."""
        cancel_event = cancel_event or threading.Event()
        if self.closed or self._stop.is_set():
            raise RuntimeError('robot is closed')
        config = load_yaml_config(self.config_path)
        if not isinstance(config, dict):
            raise ValueError('connector device config must be an object')
        safety = safety_settings(config.get('safety', {}), fallback=self.rc_config.get('safety', {}))
        for key in ('connect_timeout_s', 'close_timeout_s', 'feedback_timeout_s', 'camera_timeout_s'):
            value = config.get(key, 10 if key == 'connect_timeout_s' else 2)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f'connector {key} must be finite and positive')
        for key in ('initial_qpos', 'initial_epos'):
            if key in config:
                values = config[key]
                if (not isinstance(values, list) or len(values) != 14 or
                        any(type(v) not in (int, float) or not math.isfinite(v) for v in values)):
                    raise ValueError(f'connector {key} requires 14 finite numbers')
        if 'image_size' in config:
            values = config['image_size']
            if not isinstance(values, list) or len(values) != 2 or any(type(v) is not int or v <= 0 for v in values):
                raise ValueError('connector image_size requires two positive integers')
        if not (self.directory / 'main.py').is_file():
            raise ValueError('connector source main.py is missing')
        deadline = deadline if deadline is not None else time.monotonic() + float(config.get('connect_timeout_s', 10)) + 5
        if cancel_event.is_set() or time.monotonic() >= deadline:
            raise TimeoutError('connector preparation cancelled or timed out')
        if self.runtime is not None:
            self._prepared_spec = self.runtime.prepare_connector_spec(
                self.instance_id, self.directory, config, cancel_event, deadline)
        self.config = copy.deepcopy(config)
        self.safety = safety
        self._prepared = True

    def connect(self, cancel_event=None, *, deadline=None):
        cancel_event = cancel_event or threading.Event()
        if self.closed or self._stop.is_set(): raise RuntimeError('robot is closed')
        if not self._prepared:
            self.prepare(cancel_event, deadline=deadline)
        deadline = min(deadline if deadline is not None else float('inf'), time.monotonic() + float(self.config.get('connect_timeout_s', 10)) + 5)
        self._check_startup(deadline, cancel_event)
        if self.runtime is not None:
            try:
                self.handle = self.runtime.start_connector(self.instance_id, self.directory, self.config, cancel_event, deadline, prepared=self._prepared_spec)
            except BaseException:
                self.handle = self.runtime.connector_attempt(self._prepared_spec)
                raise
            endpoint = self.handle.socket
        else:
            self._temporary = tempfile.TemporaryDirectory(prefix='rc-connector-', dir='/tmp')
            endpoint = self._temporary.name + '/io'
            sdk = Path(__file__).resolve().parents[2] / 'packages/connector-sdk/src'
            env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, (str(sdk), os.environ.get('PYTHONPATH')))))
            self.process = subprocess.Popen([sys.executable, '-m', 'rc_connector_sdk',
                '--source', str(self.directory / 'main.py'), '--socket', endpoint,
                '--instance-id', self.instance_id, '--config-json', json.dumps(self.config),
                '--journal', self._temporary.name + '/pool.json'], env=env, start_new_session=True)
        try:
            while not Path(endpoint + '.ready').exists():
                if cancel_event.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError('connector startup cancelled or timed out')
                if self.process is not None and self.process.poll() is not None:
                    raise RuntimeError('connector process exited before connecting')
                time.sleep(.01)
            self.client = ConnectorClient(endpoint, wake=lambda: self.wake(), deadline=deadline, cancel_event=cancel_event)
            if self.client.info['instance_id'] != self.instance_id:
                raise RuntimeError('connector instance mismatch')
            if self.runtime is not None:
                self.runtime.connector_ready(self.handle, self.client.info)
            self._sample(deadline=deadline, cancel_event=cancel_event)
            self.read_state()
            self._check_startup(deadline, cancel_event)
            self._sampler = threading.Thread(target=self._collect, name='rc-connector-cache', daemon=True)
            self._sampler.start()
        except BaseException:
            self.close(deadline=deadline)
            raise

    @staticmethod
    def _check_startup(deadline, cancel_event):
        if ((deadline is not None and time.monotonic() >= deadline)
                or (cancel_event is not None and cancel_event.is_set())):
            raise TimeoutError('connector startup cancelled or deadline exceeded')

    def _sample(self, *, deadline=None, cancel_event=None):
        with self._lock:
            since = {arm: rows[-1]['seq'] if rows else 0 for arm, rows in self._history.items()}
            images = self._image_requested
            self._image_requested = False
        state = self.client.get_state(since, deadline=deadline, cancel_event=cancel_event)
        received = time.monotonic()
        self._check_startup(deadline, cancel_event)
        cameras = tuple(self.config.get('camera_topics', {'head': '', 'left_wrist': '', 'right_wrist': ''}))
        copied = None
        if images and cameras:
            result = self.client.get_images(cameras, deadline=deadline, cancel_event=cancel_event)
            lease = result['lease_id']
            try:
                copied = {}
                if result['images']:
                    with map_images(result['images']) as frames:
                        for name in frames:
                            self._check_startup(deadline, cancel_event)
                            pixels = frames[name].copy()
                            pixels.flags.writeable = False
                            copied[name] = {**result['metadata'][name], 'image': pixels}
            finally:
                # Cancellation still permits releasing a known lease, within
                # the original deadline. No acknowledgement means no proof.
                if lease is not None: self.client.release_images([lease], deadline=deadline)
        self._check_startup(deadline, cancel_event)
        with self._lock:
            self._check_startup(deadline, cancel_event)
            self._state, self._state_error, self._state_received = state['state'], state['state_error'], received
            self._feedback_errors = state.get('feedback_errors', {})
            for arm, rows in state.get('history', {}).items():
                if state.get('gaps', {}).get(arm): self._history[arm].clear()
                self._history[arm].extend(rows)
            if copied is not None:
                self._frames.update(copied)
                self._images_checked_at = time.monotonic()
                self._images_generation += 1

    def _collect(self):
        while not self._stop.wait(min(.01, 1 / self.rc_config.get('frequency_hz', 30))):
            try: self._sample()
            except Exception as exc:
                with self._lock: self._state_error = str(exc)
                self.wake()
                return

    def read_state(self):
        with self._lock:
            if self.closed or self._state_error: raise RuntimeError(self._state_error or 'robot is closed')
            state = copy.deepcopy(self._state)
            elapsed = time.monotonic() - self._state_received
            ages = {arm: value + elapsed for arm, value in state.get('feedback_age_s', {}).items()}
            if elapsed > self.config.get('feedback_timeout_s', 2) or any(v > self.config.get('feedback_timeout_s', 2) for v in ages.values()):
                raise RuntimeError('stale connector feedback')
            state['feedback_age_s'] = ages
            return state

    def _frame_snapshot(self):
        with self._lock:
            self._image_requested = True
            now = time.monotonic()
            return {name: {**frame, 'valid': bool(frame.get('valid', True) and not self.closed and
                        now - frame['received'] <= self.config.get('camera_timeout_s', 2)),
                        'error': frame.get('error') or ('stale camera frame' if self.closed or
                            now - frame['received'] > self.config.get('camera_timeout_s', 2) else None)}
                    for name, frame in self._frames.items()}

    def image_snapshot(self):
        frames = self._frame_snapshot()
        return ({name: (frame['image'], frame['timestamp']) for name, frame in frames.items() if frame['valid']},
                {name: frame['error'] for name, frame in frames.items() if not frame['valid']})

    def read_images(self):
        if self.config is None or self.closed: raise RuntimeError('connector is not connected')
        images, errors = self.image_snapshot()
        expected = set(self.config.get('camera_topics', {'head': '', 'left_wrist': '', 'right_wrist': ''}))
        if errors or expected - set(images):
            with self._lock:
                if self._state_error: raise RuntimeError(self._state_error)
                if self._required_image_generation is None:
                    self._required_image_generation = self._images_generation + 1
                if self._images_generation < self._required_image_generation:
                    raise ImagesPending('waiting for requested image snapshot')
            raise RuntimeError(f'missing or stale camera images: {errors or expected-set(images)}')
        with self._lock: self._required_image_generation = None
        return images

    def wait_images(self, timeout=2.):
        deadline = time.monotonic() + timeout
        while True:
            try: return self.read_images()
            except ImagesPending:
                if self._stop.is_set() or time.monotonic() >= deadline: raise
                time.sleep(.005)

    def camera_preview_snapshot(self): return self._frame_snapshot()

    def recording_snapshot(self):
        frames = self._frame_snapshot()
        try: state, error = self.read_state(), None
        except RuntimeError as exc:
            with self._lock: state = copy.deepcopy(self._state)
            error = str(exc)
        return {'state': state, 'state_error': error, 'state_valid': error is None,
                'frames': frames, 'feedback_errors': dict(self._feedback_errors)}

    def feedback_since(self, arm, seq):
        self.read_state()
        with self._lock:
            rows = [copy.deepcopy(row) for row in self._history[arm] if row['seq'] > seq]
            if seq and rows and rows[0]['seq'] != seq + 1:
                raise RuntimeError('feedback history gap')
            return rows

    def set_command_observer(self, callback): self._command_observer = callback
    def execute(self, action, mode='qpos'): return self.execute_arms(arm_targets(action, mode))

    def execute_arms(self, targets):
        if self.closed or self._stop.is_set() or self.client is None: raise RuntimeError('connector is not connected')
        identity = self.client.submit(targets)
        self.last_submission = identity
        with self._result_lock: self._sent[identity] = copy.deepcopy(targets)
        return identity

    def poll_results(self):
        if self.client is None: return []
        results = self.client.poll_results()
        self._record_results(results)
        return results

    def bind_attempt(self, identity, attempt):
        with self._result_lock: self._attempts[identity] = attempt

    def _record_results(self, results):
        with self._result_lock: self._record_results_locked(results)

    def _record_results_locked(self, results):
        for response in results:
            self._receipts[response['id']] = response
            targets = self._sent.pop(response['id'], {})
            if response['success']:
                state = self._state or {'qpos': [0.] * 14}
                values = list(state['qpos'])
                for arm, target in targets.items():
                    i = 0 if arm == 'left' else 7
                    values[i:i+7] = target['values']
                self._commands.append(np.asarray(values))
                self._command_modes.append({arm: t['mode'] for arm, t in targets.items()})
            attempt = self._attempts.pop(response['id'], None)
            for event in response.get('events', []):
                if self._command_observer:
                    try: self._command_observer({**event, 'attempt_id': attempt, 'connector_request_id': response['id']})
                    except Exception: pass
            while len(self._receipts) > 512: self._receipts.pop(next(iter(self._receipts)))

    def publication(self, identity):
        with self._result_lock: return self._receipts.get(identity)

    def close(self, *, deadline=None):
        if self.closed: return
        deadline = deadline if deadline is not None else time.monotonic() + 5
        soft_deadline = deadline - min(.25, max(0, deadline - time.monotonic()) / 4)
        self._stop.set()
        failures = []
        confirmed = True
        if self._sampler is not None:
            self._sampler.join(max(0, soft_deadline - time.monotonic()))
            if self._sampler.is_alive():
                failures.append('connector image reader did not stop')
                confirmed = False
        if self.client is not None and not failures and not self._client_close_confirmed:
            try:
                if self.client.closed:
                    raise RuntimeError(self._client_close_error or 'connector reader release unconfirmed')
                self.client.close(timeout=max(.001, soft_deadline - time.monotonic()), on_results=self._record_results)
                self._client_close_confirmed = True
            except Exception as exc:
                self._client_close_error = str(exc)
                failures.append(str(exc))
                confirmed = False
        if self.runtime is not None and self.handle is not None:
            try:
                proof = self.runtime.stop_and_remove(self.handle, deadline)
                if proof.forced:
                    self.cleanup_forced = True
                    failures.append('connector exited abnormally or required forced cleanup')
                if not failures and self.client is not None:
                    self.runtime.forget_pool(self.client.info['pool_id'])
            except Exception as exc:
                failures.append(str(exc))
                confirmed = False
        if self.process is not None:
            try: self.process.wait(timeout=max(.001, soft_deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try: os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                try: self.process.wait(timeout=max(.001, deadline - time.monotonic()) / 2)
                except subprocess.TimeoutExpired:
                    try: os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    try: self.process.wait(timeout=max(.001, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired:
                        failures.append('connector process exit unconfirmed')
                        confirmed = False
                self.cleanup_forced = True
                failures.append('connector required forced termination')
            if self.process.returncode:
                self.cleanup_forced = True
                failures.append(f'connector exited {self.process.returncode}')
            # The connector owns a process session. Reaping only its leader is
            # insufficient when a driver has spawned children.
            def group_alive():
                try:
                    os.killpg(self.process.pid, 0)
                    return True
                except ProcessLookupError:
                    return False
            if group_alive():
                self.cleanup_forced = True
                failures.append('connector process tree required forced termination')
                try: os.killpg(self.process.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                kill_at = time.monotonic() + max(0., deadline - time.monotonic()) / 2
                killed = False
                while group_alive() and time.monotonic() < deadline:
                    if not killed and time.monotonic() >= kill_at:
                        try: os.killpg(self.process.pid, signal.SIGKILL)
                        except ProcessLookupError: pass
                        killed = True
                    time.sleep(min(.005, max(0., deadline - time.monotonic())))
                if group_alive():
                    confirmed = False
                    failures.append('connector process tree exit unconfirmed')
        if self.client is not None: self.client.disconnect(timeout=max(0., deadline - time.monotonic()))
        self.closed = confirmed
        if self.closed and self._temporary is not None: self._temporary.cleanup()
        if failures: raise RuntimeError('; '.join(failures))
