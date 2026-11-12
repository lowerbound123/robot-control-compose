"""ROS 1 LIFT2 device I/O. Scheduling, ownership and image leases belong to RC/SDK."""
from collections import deque
import copy
import io
import ipaddress
import math
import os
import threading
import time
from types import SimpleNamespace
from urllib.parse import urlparse
import uuid
import warnings

import numpy as np

from rc_connector_sdk.actions import arm_targets, validate_arm_targets


ARMS = ('left', 'right')
DEFAULT_CAMERA_TOPICS = {
    'head': '/camera_h/color/image_raw',
    'left_wrist': '/camera_l/color/image_raw',
    'right_wrist': '/camera_r/color/image_raw',
}


def _load_ros():
    try:
        import rospy
        from arm_control.msg import JointInformation, JointControl, PosCmd
        from sensor_msgs.msg import Image, CompressedImage
    except ImportError as exc:
        raise RuntimeError('LIFT2 requires its ROS 1 connector image and arm_control messages') from exc
    return SimpleNamespace(rospy=rospy, JointInformation=JointInformation, JointControl=JointControl,
                           PosCmd=PosCmd, Image=Image, CompressedImage=CompressedImage)


def _plain(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (float, np.floating)):
        if not math.isfinite(value): raise ValueError('non-finite feedback value')
        return float(value)
    if isinstance(value, np.integer): return int(value)
    if isinstance(value, dict): return {key: _plain(item) for key, item in value.items()}
    if hasattr(value, '__slots__'):
        return {key: _plain(getattr(value, key)) for key in value.__slots__}
    if isinstance(value, SimpleNamespace): return _plain(vars(value))
    return [_plain(item) for item in value]


def _timestamp(message, received):
    stamp = getattr(getattr(message, 'header', None), 'stamp', None)
    if stamp is not None:
        value = float(stamp.secs) + float(stamp.nsecs) * 1e-9
        if math.isfinite(value) and value > 0: return value
    return received


def _decode_image(message, transport):
    data = bytes(message.data)
    if len(data) > 64 * 1024 * 1024:
        raise ValueError('camera frame exceeds 64 MiB')
    if transport == 'compressed':
        from PIL import Image
        with Image.open(io.BytesIO(data)) as source:
            if source.width * source.height > 16_000_000:
                raise ValueError('camera frame exceeds 16 megapixels')
            frame = np.array(source.convert('RGB'), dtype=np.uint8)
    else:
        channels = {'rgb8': 3, 'bgr8': 3, 'rgba8': 4, 'bgra8': 4, 'mono8': 1}
        encoding = message.encoding
        if encoding not in channels: raise ValueError(f'unsupported RGB image encoding: {encoding}')
        height, width, step = message.height, message.width, message.step
        if (type(height) is not int or type(width) is not int or type(step) is not int
                or min(height, width) <= 0 or height * width > 16_000_000
                or step < width * channels[encoding] or len(data) != height * step):
            raise ValueError('invalid camera dimensions, row stride or data length')
        rows = np.frombuffer(data, dtype=np.uint8).reshape(height, step)
        pixels = rows[:, :width * channels[encoding]].reshape(height, width, channels[encoding])
        if encoding == 'mono8': frame = np.repeat(pixels, 3, axis=2)
        elif encoding in ('bgr8', 'bgra8'): frame = pixels[:, :, [2, 1, 0]].copy(order='C')
        else: frame = pixels[:, :, :3].copy(order='C')
    frame.flags.writeable = False
    return frame


class Lift2Robot:
    def __init__(self, config=None):
        self.config = copy.deepcopy(dict(config or {}))
        master = self.config.get('ros_master_uri', '')
        if not isinstance(master, str): raise ValueError('ros_master_uri must be a string URI')
        parsed = urlparse(master)
        try:
            valid_master = (parsed.scheme == 'http' and parsed.hostname and parsed.port
                            and not parsed.username and not parsed.password
                            and parsed.path in ('', '/') and not parsed.query and not parsed.fragment)
        except ValueError:
            valid_master = False
        if not valid_master: raise ValueError('ros_master_uri must be an explicit http://host:port URI')
        address = self.config.get('ros_ip', '')
        if not isinstance(address, str): raise ValueError('ros_ip must be a string IP address')
        try: ipaddress.ip_address(address)
        except ValueError as exc: raise ValueError('ros_ip must be an explicit local IP address') from exc
        self.camera_topics = dict(self.config.get('camera_topics', DEFAULT_CAMERA_TOPICS))
        if any(not isinstance(k, str) or not k or not isinstance(v, str) or not v
               for k, v in self.camera_topics.items()):
            raise ValueError('camera_topics must map camera names to nonempty ROS topics')
        self._transport = self.config.get('camera_transport', 'raw')
        if self._transport not in ('raw', 'compressed'): raise ValueError('camera_transport must be raw or compressed')
        self._feedback_timeout = self._timeout('feedback_timeout_s', 2)
        self._camera_timeout = self._timeout('camera_timeout_s', 2)
        self._connect_timeout = self._timeout('connect_timeout_s', 10)
        self._close_timeout = self._timeout('close_timeout_s', 2)
        self._condition = threading.Condition()
        self._resources = threading.RLock()
        self._close_lock = threading.Lock()
        self._stop = threading.Event()
        self._feedback, self._eef, self._feedback_errors = {}, {}, {}
        self._sequences = dict.fromkeys(ARMS, 0)
        self._history = {arm: deque(maxlen=512) for arm in ARMS}
        self._images, self._pending_images, self._image_errors = {}, {}, {}
        self._image_sequences = dict.fromkeys(self.camera_topics, 0)
        self._image_processed = dict.fromkeys(self.camera_topics, 0)
        self._recording_state = None
        self._observer = None
        self._binding = None
        self._publishers, self._subscriptions = {}, []
        self._decoder = self._cleanup_thread = self._initializer = None
        self._initialized = threading.Event()
        self._initialize_error = None
        self._cleanup_error = None
        self._connected = self._connecting = self.closed = False

    def _timeout(self, key, default):
        value = self.config.get(key, default)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f'{key} must be finite and positive')
        return float(value)

    def _cancelled(self, cancel):
        if self._stop.is_set() or (cancel is not None and cancel.is_set()):
            raise RuntimeError('LIFT2 connection cancelled or closed')

    def connect(self, cancel_event=None):
        deadline = time.monotonic() + self._connect_timeout
        with self._resources:
            self._cancelled(cancel_event)
            if self._connected: return
            if self._connecting: raise RuntimeError('LIFT2 connection already in progress')
            self._connecting = True
        try:
            os.environ['ROS_MASTER_URI'] = self.config['ros_master_uri']
            os.environ['ROS_IP'] = self.config['ros_ip']
            # ROS_HOSTNAME wins over ROS_IP in rospy; never inherit a stale hostname.
            os.environ.pop('ROS_HOSTNAME', None)
            os.environ['ROS_HOME'] = '/tmp/rc-lift2-ros'
            os.environ['ROS_LOG_DIR'] = '/tmp/rc-lift2-ros/log'
            # rosgraph otherwise tries to chown new directories to /tmp's
            # owner, which a non-root, capability-free container cannot do.
            os.makedirs(os.environ['ROS_LOG_DIR'], mode=0o700, exist_ok=True)
            binding = _load_ros()
            # init_node can wait indefinitely for a missing master. The caller
            # retains its startup deadline even when ROS cannot be interrupted.
            self._initializer = threading.Thread(target=self._initialize_ros, args=(binding, cancel_event),
                                                 name='lift2-ros-init', daemon=True)
            self._initializer.start()
            while not self._initialized.wait(min(.02, max(0., deadline-time.monotonic()))):
                self._cancelled(cancel_event)
                if time.monotonic() >= deadline: raise RuntimeError('LIFT2 ROS initialization timed out')
            if self._initialize_error: raise self._initialize_error
            with self._condition:
                while True:
                    self._cancelled(cancel_event)
                    if time.monotonic() >= deadline: raise RuntimeError('LIFT2 connect deadline exceeded')
                    try:
                        self._read_state_locked()
                        for side, publisher in self._publishers.items():
                            if publisher.get_num_connections() < 1:
                                raise RuntimeError(f'{side} command topic has no subscriber')
                    except RuntimeError as exc: error = str(exc)
                    else:
                        self._connected = True
                        return
                    remaining = deadline - time.monotonic()
                    if remaining <= 0: raise RuntimeError('LIFT2 connect timed out: ' + error)
                    self._condition.wait(min(.05, remaining))
        except BaseException:
            self.close(deadline=deadline)
            raise
        finally:
            self._connecting = False

    def _initialize_ros(self, binding, cancel_event):
        try:
            with self._resources:
                self._cancelled(cancel_event)
                self._binding = binding
                binding.rospy.init_node('rc_lift2_' + uuid.uuid4().hex[:8], argv=[],
                                        disable_signals=True, disable_rosout=True)
                self._cancelled(cancel_event)
                for side, suffix in (('left', ''), ('right', '2')):
                    joint_topic = self.config.get(f'feedback_{side}_topic', '/joint_information' + suffix)
                    pose_topic = self.config.get(f'eef_{side}_topic', '/follow' + ('1' if side == 'left' else '2') + '_pos_back')
                    command_topic = self.config.get(f'command_{side}_topic', '/joint_control' + suffix)
                    self._subscriptions.append(binding.rospy.Subscriber(joint_topic, binding.JointInformation,
                        lambda msg, side=side: self._on_joint(side, msg), queue_size=10, tcp_nodelay=True))
                    self._subscriptions.append(binding.rospy.Subscriber(pose_topic, binding.PosCmd,
                        lambda msg, side=side: self._on_eef(side, msg), queue_size=10, tcp_nodelay=True))
                    # SDK owns the bounded FIFO. rospy's bounded async publisher
                    # discards old commands, so use synchronous publication.
                    with warnings.catch_warnings():
                        warnings.filterwarnings('ignore', message='The publisher should be created.*', category=SyntaxWarning)
                        self._publishers[side] = binding.rospy.Publisher(command_topic, binding.JointControl,
                            queue_size=None, latch=False, tcp_nodelay=True)
                image_type = binding.Image if self._transport == 'raw' else binding.CompressedImage
                for camera, topic in self.camera_topics.items():
                    self._subscriptions.append(binding.rospy.Subscriber(topic, image_type,
                        lambda msg, camera=camera: self._on_image(camera, msg), queue_size=1,
                        buff_size=64 * 1024 * 1024, tcp_nodelay=True))
                self._cancelled(cancel_event)
                self._decoder = threading.Thread(target=self._decode_images, name='lift2-image-decode', daemon=True)
                self._decoder.start()
        except BaseException as exc:
            self._initialize_error = exc
        finally:
            self._initialized.set()

    def _on_joint(self, side, message):
        self._feedback_received(side, message, 'joint')

    def _on_eef(self, side, message):
        self._feedback_received(side, message, 'eef')

    def _feedback_received(self, side, message, kind):
        if self._stop.is_set(): return
        received, timestamp = time.monotonic(), time.time()
        key = side + '.' + kind
        cache = self._feedback if kind == 'joint' else self._eef
        try:
            raw = _plain(message)
            if kind == 'joint':
                for field in ('joint_pos', 'joint_vel', 'joint_cur'):
                    values = raw[field]
                    if len(values) != 7 or not all(type(x) in (int, float) and math.isfinite(x) for x in values):
                        raise ValueError(f'{field} requires 7 finite numbers')
            else:
                for field in ('x', 'y', 'z', 'roll', 'pitch', 'yaw', 'gripper'):
                    if type(raw[field]) not in (int, float) or not math.isfinite(raw[field]):
                        raise ValueError(f'{field} must be finite')
            sample = (raw, _timestamp(message, timestamp), received)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            with self._condition:
                cache.pop(side, None)
                self._feedback_errors[key] = str(exc)
                self._condition.notify_all()
            return
        with self._condition:
            if self._stop.is_set(): return
            cache[side] = sample
            self._feedback_errors.pop(key, None)
            if kind == 'joint': self._sequences[side] += 1
            if side in self._feedback and side in self._eef:
                joint_sample = self._feedback[side]
                row = {'seq': self._sequences[side], 'received': joint_sample[2],
                       'qpos': list(joint_sample[0]['joint_pos']), 'epos': self._pose(side)}
                history = self._history[side]
                if not history or history[-1]['seq'] != row['seq']: history.append(row)
            if all(arm in self._feedback and arm in self._eef for arm in ARMS):
                self._recording_state = self._cached_state_locked()
            self._condition.notify_all()

    def _pose(self, side):
        return [self._eef[side][0][field] for field in ('x', 'y', 'z', 'roll', 'pitch', 'yaw')] + [self._feedback[side][0]['joint_pos'][6]]

    def _cached_state_locked(self):
        now = time.monotonic()
        timestamps = {side: self._feedback[side][1] for side in ARMS}
        poses = {side: self._eef[side][1] for side in ARMS}
        return {
            'qpos': sum((list(self._feedback[side][0]['joint_pos']) for side in ARMS), []),
            'qvel': sum((list(self._feedback[side][0]['joint_vel']) for side in ARMS), []),
            'effort': sum((list(self._feedback[side][0]['joint_cur']) for side in ARMS), []),
            'eef': self._pose('left') + self._pose('right'),
            'timestamp': min(*timestamps.values(), *poses.values()), 'timestamps': timestamps,
            'eef_timestamps': poses, 'sequences': dict(self._sequences),
            'feedback_age_s': {side: now-min(self._feedback[side][2], self._eef[side][2]) for side in ARMS},
            'raw': {side: {'joint': copy.deepcopy(self._feedback[side][0]),
                           'eef': copy.deepcopy(self._eef[side][0])} for side in ARMS},
        }

    def _read_state_locked(self):
        if self.closed or self._stop.is_set(): raise RuntimeError('LIFT2 is closed')
        if self._binding and self._binding.rospy.is_shutdown(): raise RuntimeError('ROS 1 stopped')
        now = time.monotonic()
        for side in ARMS:
            for kind, cache in (('joint', self._feedback), ('eef', self._eef)):
                if side not in cache:
                    raise RuntimeError(f'missing {side} {kind} feedback: ' + self._feedback_errors.get(side+'.'+kind, 'no sample'))
                if now-cache[side][2] > self._feedback_timeout:
                    raise RuntimeError(f'stale {side} {kind} feedback')
        return self._cached_state_locked()

    def _check_ready(self):
        if self.closed or self._stop.is_set(): raise RuntimeError('LIFT2 is closed')
        if not self._connected: raise RuntimeError('LIFT2 must connect before use')
        if self._binding.rospy.is_shutdown(): raise RuntimeError('ROS 1 stopped')

    def read_state(self):
        with self._condition:
            self._check_ready()
            return self._read_state_locked()

    def feedback_since(self, arm, seq):
        with self._condition:
            self._check_ready()
            self._read_state_locked()
            return copy.deepcopy([row for row in self._history[arm] if row['seq'] > seq])

    def _on_image(self, camera, message):
        if self._stop.is_set(): return
        received, timestamp = time.monotonic(), time.time()
        with self._condition:
            self._image_sequences[camera] += 1
            seq = self._image_sequences[camera]
            try:
                if len(message.data) > 64 * 1024 * 1024: raise ValueError('camera frame exceeds 64 MiB')
                snapshot = SimpleNamespace(data=bytes(message.data))
                if self._transport == 'raw':
                    for field in ('height', 'width', 'encoding', 'step', 'is_bigendian'):
                        setattr(snapshot, field, getattr(message, field))
                self._pending_images[camera] = (snapshot, _timestamp(message, timestamp), received, seq)
            except (ValueError, TypeError, AttributeError) as exc:
                self._pending_images.pop(camera, None)
                self._image_processed[camera] = seq
                self._image_errors[camera] = str(exc)
            self._condition.notify_all()

    def _decode_images(self):
        while not self._stop.is_set():
            with self._condition:
                while not self._pending_images and not self._stop.is_set(): self._condition.wait()
                if self._stop.is_set(): return
                camera = next(iter(self._pending_images))
                message, timestamp, received, seq = self._pending_images.pop(camera)
            try: frame, error = _decode_image(message, self._transport), None
            except Exception as exc: frame, error = None, f'cannot decode camera frame: {exc}'
            with self._condition:
                if self._stop.is_set() or seq < self._image_processed[camera]: continue
                self._image_processed[camera] = seq
                if error: self._image_errors[camera] = error
                else:
                    self._images[camera] = (frame, timestamp, received, seq)
                    self._image_errors.pop(camera, None)
                self._condition.notify_all()

    def recording_snapshot(self):
        with self._condition:
            try: self._read_state_locked(); state_error = None
            except RuntimeError as exc: state_error = str(exc)
            now = time.monotonic()
            frames = {}
            stopped = self.closed or self._stop.is_set() or (self._binding and self._binding.rospy.is_shutdown())
            for name, (image, timestamp, received, seq) in self._images.items():
                error = 'LIFT2 is closed or ROS stopped' if stopped else self._image_errors.get(name)
                if not error and now-received > self._camera_timeout: error = f'stale camera {name}'
                frames[name] = {'image': image, 'timestamp': timestamp, 'received': received,
                                'sequence': seq, 'valid': error is None, 'error': error}
            return {'state': copy.deepcopy(self._recording_state), 'state_error': state_error,
                    'state_valid': state_error is None, 'frames': frames,
                    'feedback_errors': dict(self._feedback_errors),
                    'feedback_age_s': {side: now-min(self._feedback[side][2], self._eef[side][2])
                                       for side in ARMS if side in self._feedback and side in self._eef}}

    def camera_preview_snapshot(self):
        return {name: {key: frame[key] for key in ('image', 'sequence', 'received')}
                for name, frame in self.recording_snapshot()['frames'].items()}

    def image_snapshot(self):
        with self._condition:
            self._check_ready()
            frames = self.recording_snapshot()['frames']
            images, errors = {}, {}
            for camera in self.camera_topics:
                frame = frames.get(camera)
                if frame and frame['valid']: images[camera] = (frame['image'], frame['timestamp'])
                else: errors[camera] = self._image_errors.get(camera) or (frame['error'] if frame else 'missing camera frame')
            return images, errors

    def read_images(self):
        images, errors = self.image_snapshot()
        if errors: raise RuntimeError(f'missing or stale camera images: {errors}')
        return images

    def set_command_observer(self, callback): self._observer = callback

    def execute(self, action, mode='qpos'): return self.execute_arms(arm_targets(action, mode))

    def execute_arms(self, targets):
        targets = validate_arm_targets(targets)
        if any(target['mode'] != 'qpos' for target in targets.values()):
            raise ValueError('LIFT2 epos control is unsupported; entire request rejected before publication')
        if any(abs(value) > np.finfo(np.float32).max for target in targets.values() for value in target['values']):
            raise ValueError('LIFT2 joint targets must fit finite ROS float32 values')
        with self._resources:
            self._check_ready()
            for side in targets:
                if self._publishers[side].get_num_connections() < 1:
                    raise RuntimeError(f'{side} command topic has no subscriber')
            sent = []
            for side, target in targets.items():
                message = self._binding.JointControl()
                message.joint_pos = list(target['values'])
                message.joint_vel, message.joint_cur, message.mode = [0.]*7, [0.]*7, 0
                try: self._publishers[side].publish(message)
                except Exception as exc:
                    raise RuntimeError(f'publish failed for {side}; already published arms={sent}: {exc}') from exc
                sent.append(side)
                if self._observer:
                    try: self._observer({'arm': side, 'mode': 'qpos', 'values7': list(target['values']),
                                         'timestamp_ns': time.time_ns(), 'monotonic_ns': time.monotonic_ns()})
                    except Exception: pass

    def _cleanup(self):
        failures = []
        try:
            with self._resources:
                for resource in [*self._subscriptions, *self._publishers.values()]:
                    try: resource.unregister()
                    except Exception as exc: failures.append(str(exc))
                if self._binding:
                    try: self._binding.rospy.signal_shutdown('LIFT2 connector closed')
                    except Exception as exc: failures.append(str(exc))
                self._subscriptions.clear()
                self._publishers.clear()
        except Exception as exc:
            failures.append(str(exc))
        self._cleanup_error = '; '.join(failures) or None

    def close(self, *, deadline=None):
        deadline = min(deadline if deadline is not None else float('inf'), time.monotonic() + self._close_timeout)
        self._stop.set()
        self.closed, self._connected = True, False
        with self._condition:
            self._condition.notify_all()
        if not self._close_lock.acquire(timeout=max(0., deadline-time.monotonic())):
            raise RuntimeError('LIFT2 close deadline exceeded: concurrent cleanup')
        try:
            if self._cleanup_thread is None:
                self._cleanup_thread = threading.Thread(target=self._cleanup, name='lift2-ros-close', daemon=True)
                self._cleanup_thread.start()
            for thread in (self._cleanup_thread, self._decoder, self._initializer):
                if thread is not None and thread.ident is not None:
                    thread.join(max(0., deadline-time.monotonic()))
            if self._cleanup_error: raise RuntimeError('LIFT2 cleanup failed: ' + self._cleanup_error)
            if any(thread is not None and thread.is_alive() for thread in (self._cleanup_thread, self._decoder, self._initializer)):
                raise RuntimeError('LIFT2 close deadline exceeded: ROS or decoder did not stop')
            with self._condition:
                self._pending_images.clear()
                self._images.clear()
        finally:
            self._close_lock.release()


def create_connector(config):
    return Lift2Robot(config)
