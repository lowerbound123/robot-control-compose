"""Independent RobotStatus/RobotCmd adapter for ACOne arms and RGB cameras.

Only connect imports ROS. Callbacks retain raw feedback, while a separate
thread decodes the most recent compressed frame for each configured camera.
"""
import copy
import importlib
import io
import math
import threading
import time
import uuid
from collections.abc import Mapping
from collections import deque
from types import SimpleNamespace

import numpy as np

from .mock import validate_qpos
from ..safety import arm_targets, validate_arm_targets


DEFAULT_CAMERA_TOPICS = {
    "head": "/camera/camera_h/color/image_rect_raw/compressed",
    "left_wrist": "/camera/camera_l/color/image_rect_raw/compressed",
    "right_wrist": "/camera/camera_r/color/image_rect_raw/compressed",
}


def _load_ros(message_types):
    """Resolve installed ROS bindings at connection time, including test msgs."""
    try:
        import rclpy
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import qos_profile_sensor_data
        from rclpy.signals import SignalHandlerOptions
        from sensor_msgs.msg import CompressedImage
        resolved = []
        for message_type in message_types:
            package, category, name = message_type.split("/")
            if category != "msg":
                raise ValueError("message_type must use package/msg/Message format")
            resolved.append(getattr(importlib.import_module(f"{package}.msg"), name))
    except (ImportError, AttributeError) as error:
        raise RuntimeError(
            f"ROS 2 bindings or {message_types} are unavailable; manually install/source "
            "ROS and the arm message package before selecting the acone backend"
        ) from error
    return SimpleNamespace(
        rclpy=rclpy, Context=Context, Executor=SingleThreadedExecutor,
        message_type=resolved[0], command_type=resolved[1], CompressedImage=CompressedImage,
        sensor_qos=qos_profile_sensor_data, signal_options=SignalHandlerOptions.NO,
    )


def _raw_json(value):
    """Copy ROS fields to plain JSON values without model-specific transforms."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("ROS feedback contains a non-finite value")
        return value
    if isinstance(value, np.generic):
        return _raw_json(value.item())
    if isinstance(value, Mapping):
        return {str(key): _raw_json(item) for key, item in value.items()}
    if hasattr(value, "get_fields_and_field_types"):
        return {key: _raw_json(getattr(value, key)) for key in value.get_fields_and_field_types()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return list(value)
    try:
        return [_raw_json(item) for item in value]
    except TypeError as error:
        raise TypeError(f"unsupported ROS field type: {type(value).__name__}") from error


def _message_timestamp(message, receipt_time):
    header = getattr(message, "header", None)
    stamp = getattr(header, "stamp", None)
    if stamp is not None:
        timestamp = float(stamp.sec) + float(stamp.nanosec) * 1e-9
        if math.isfinite(timestamp) and timestamp > 0:
            return timestamp
    return receipt_time


def _decode_rgb(data):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 16_000_000:
            raise ValueError("compressed camera frame exceeds 16 megapixels")
        frame = np.array(image.convert("RGB"), dtype=np.uint8)
    frame.flags.writeable = False
    return frame


class ACOneRobot:
    def __init__(self, config=None):
        self.config = dict(config or {})
        self.camera_topics = dict(self.config.get("camera_topics", DEFAULT_CAMERA_TOPICS))
        self._feedback_timeout = self._timeout("feedback_timeout_s", 2.0)
        self._camera_timeout = self._timeout("camera_timeout_s", 2.0)
        self._connect_timeout = self._timeout("connect_timeout_s", 10.0)
        self._close_timeout = self._timeout("close_timeout_s", 2.0)
        self._condition = threading.Condition()
        self._resources_lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._stop = threading.Event()
        self._feedback = {}
        self._sequences = {"left": 0, "right": 0}
        self._history = {arm: deque(maxlen=512) for arm in self._sequences}
        self._feedback_errors = {}
        self._images = {}
        self._image_sequences = {}
        self._image_errors = {}
        self._pending_images = {}
        self._fatal_error = None
        self._connected = False
        self._connecting = False
        self.closed = False
        self._cleanup_done = False
        self._bindings = self._context = self._node = self._executor = None
        self._publishers = {}
        self._spin_thread = self._decoder_thread = None

    def _timeout(self, key, default):
        value = float(self.config.get(key, default))
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{key} must be a finite positive number")
        return value

    def connect(self, cancel_event=None):
        with self._resources_lock:
            if self.closed or self._stop.is_set():
                raise RuntimeError("robot is closed")
            if self._connected:
                return
            if self._connecting:
                raise RuntimeError("robot connection is already in progress")
            self._connecting = True
        try:
            self._check_connect_cancel(cancel_event)
            binding = _load_ros((
                self.config.get("feedback_message_type", self.config.get("message_type", "arx5_arm_msg/msg/RobotStatus")),
                self.config.get("command_message_type", "arx5_arm_msg/msg/RobotCmd"),
            ))
            with self._resources_lock:
                self._check_connect_cancel(cancel_event)
                self._bindings = binding
                self._context = binding.Context()
                self._check_connect_cancel(cancel_event)
                binding.rclpy.init(args=[], context=self._context, signal_handler_options=binding.signal_options)
                self._check_connect_cancel(cancel_event)
                self._node = binding.rclpy.create_node(
                    "robot_control_" + uuid.uuid4().hex[:8], context=self._context,
                    namespace=self.config.get("namespace", "/"), use_global_arguments=False,
                )
                self._executor = binding.Executor(context=self._context)
                self._executor.add_node(self._node)
                for side, suffix in (("left", "l"), ("right", "r")):
                    feedback_topic = self.config.get(f"feedback_{side}_topic", f"/arm_{suffix}_status")
                    command_topic = self.config.get(f"command_{side}_topic", f"/arm_{suffix}_cmd")
                    self._node.create_subscription(
                        binding.message_type, feedback_topic,
                        lambda message, side=side: self._on_feedback(side, message), binding.sensor_qos,
                    )
                    self._publishers[side] = self._node.create_publisher(binding.command_type, command_topic, 10)
                for camera, topic in self.camera_topics.items():
                    self._node.create_subscription(
                        binding.CompressedImage, topic,
                        lambda message, camera=camera: self._on_image(camera, message), binding.sensor_qos,
                    )
                self._decoder_thread = threading.Thread(
                    target=self._decode_images, name="acone-image-decode", daemon=True,
                )
                self._spin_thread = threading.Thread(target=self._spin, name="acone-ros", daemon=True)
                self._decoder_thread.start()
                self._spin_thread.start()
            deadline = time.monotonic() + self._connect_timeout
            last_error = "missing feedback"
            with self._condition:
                while not self._stop.is_set():
                    self._check_connect_cancel(cancel_event)
                    try:
                        self._read_state_locked()
                    except RuntimeError as error:
                        last_error = str(error)
                    else:
                        self._connected = True
                        return
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError(f"robot connect timed out: {last_error}")
                    self._condition.wait(min(remaining, 0.05))
                raise RuntimeError("robot connection cancelled by close")
        except Exception:
            self.close()
            raise
        finally:
            self._connecting = False

    def _check_connect_cancel(self, cancel_event):
        if self._stop.is_set() or (cancel_event is not None and cancel_event.is_set()):
            raise RuntimeError("robot connection cancelled")

    def _on_feedback(self, side, message):
        if self._stop.is_set():
            return
        try:
            raw = _raw_json(message)
            for field, length in (("joint_pos", 7), ("joint_vel", 7), ("joint_cur", 7), ("end_pos", 6)):
                if len(raw[field]) != length or not all(math.isfinite(float(value)) for value in raw[field]):
                    raise ValueError(f"{field} requires {length} finite values")
            sample = (raw, _message_timestamp(message, time.time()), time.monotonic())
        except (ValueError, TypeError, KeyError) as error:
            with self._condition:
                self._feedback.pop(side, None)
                self._feedback_errors[side] = str(error)
                self._condition.notify_all()
            return
        with self._condition:
            self._feedback[side] = sample
            self._sequences[side] += 1
            self._history[side].append({"seq": self._sequences[side], "received": sample[2],
                                       "qpos": list(raw["joint_pos"]), "epos": list(raw["end_pos"]) + [raw["joint_pos"][-1]]})
            self._feedback_errors.pop(side, None)
            self._condition.notify_all()

    def _on_image(self, camera, message):
        if self._stop.is_set():
            return
        received = time.monotonic()
        # At most one waiting compressed sample per camera. Decoding never runs
        # on the ROS executor or the control thread.
        data = bytes(message.data)
        if len(data) > 32 * 1024 * 1024:
            with self._condition:
                self._image_errors[camera] = "compressed frame exceeds 32 MiB"
                self._condition.notify_all()
            return
        with self._condition:
            self._pending_images[camera] = (data, _message_timestamp(message, time.time()), received)
            self._condition.notify_all()

    def _decode_images(self):
        while not self._stop.is_set():
            with self._condition:
                while not self._pending_images and not self._stop.is_set():
                    self._condition.wait()
                if self._stop.is_set():
                    break
                camera = next(iter(self._pending_images))
                data, timestamp, received = self._pending_images.pop(camera)
            try:
                frame = _decode_rgb(data)
            except Exception as error:
                with self._condition:
                    self._image_errors[camera] = f"cannot decode compressed image: {error}"
                    self._condition.notify_all()
                continue
            with self._condition:
                if not self._stop.is_set():
                    self._images[camera] = (frame, timestamp, received)
                    self._image_sequences[camera] = self._image_sequences.get(camera, 0) + 1
                    self._image_errors.pop(camera, None)
                    self._condition.notify_all()

    def _spin(self):
        try:
            while not self._stop.is_set() and self._context.ok():
                self._executor.spin_once(timeout_sec=0.1)
            if not self._stop.is_set():
                raise RuntimeError("ROS context stopped")
        except Exception as error:
            if not self._stop.is_set():
                with self._condition:
                    self._fatal_error = f"ROS executor failed: {error}"
                    self._condition.notify_all()

    def _check_ready(self):
        if self.closed or self._stop.is_set():
            raise RuntimeError("robot is closed")
        if not self._connected:
            raise RuntimeError("robot must connect before use")

    def _read_state_locked(self):
        if self._fatal_error:
            raise RuntimeError(self._fatal_error)
        now = time.monotonic()
        for side in ("left", "right"):
            if side not in self._feedback:
                detail = self._feedback_errors.get(side, "no sample received")
                raise RuntimeError(f"missing {side} arm feedback: {detail}")
            if now - self._feedback[side][2] > self._feedback_timeout:
                raise RuntimeError(f"stale {side} arm feedback")
        left, right = (self._feedback[side][0] for side in ("left", "right"))
        timestamps = {side: self._feedback[side][1] for side in ("left", "right")}
        return {
            "qpos": list(left["joint_pos"]) + list(right["joint_pos"]),
            "qvel": list(left["joint_vel"]) + list(right["joint_vel"]),
            "effort": list(left["joint_cur"]) + list(right["joint_cur"]),
            "eef": list(left["end_pos"]) + [left["joint_pos"][-1]] + list(right["end_pos"]) + [right["joint_pos"][-1]],
            "timestamp": min(timestamps.values()), "timestamps": timestamps,
            "sequences": dict(self._sequences),
            "feedback_age_s": {side: now - self._feedback[side][2] for side in ("left", "right")},
            "raw": {"left": copy.deepcopy(left), "right": copy.deepcopy(right)},
        }

    def read_state(self):
        with self._condition:
            self._check_ready()
            return self._read_state_locked()

    def _read_images_locked(self):
        if self._fatal_error:
            raise RuntimeError(self._fatal_error)
        now = time.monotonic()
        result = {}
        for camera in self.camera_topics:
            if camera not in self._images:
                detail = self._image_errors.get(camera, "no decoded sample received")
                raise RuntimeError(f"missing camera {camera}: {detail}")
            frame, timestamp, received = self._images[camera]
            if now - received > self._camera_timeout:
                raise RuntimeError(f"stale camera {camera}")
            result[camera] = (frame, timestamp)
        return result

    def read_images(self):
        with self._condition:
            self._check_ready()
            return self._read_images_locked()

    def image_snapshot(self):
        with self._condition:
            self._check_ready()
            images, errors = {}, {}
            now = time.monotonic()
            for camera in self.camera_topics:
                sample = self._images.get(camera)
                if sample is None or now - sample[2] > self._camera_timeout:
                    errors[camera] = self._image_errors.get(camera, "missing or stale camera frame")
                else:
                    images[camera] = sample[:2]
            return images, errors

    def camera_preview_snapshot(self):
        """Keep stale frames available for display, with their original age."""
        with self._condition:
            # ROS subscriptions run while connect() waits for initial arm
            # feedback. Preview must not depend on that control-readiness gate.
            if self.closed or self._stop.is_set():
                raise RuntimeError("robot is closed")
            return {name: {"image": sample[0], "sequence": self._image_sequences[name], "received": sample[2]}
                    for name, sample in self._images.items()}

    def feedback_since(self, arm, seq):
        with self._condition:
            self._check_ready()
            self._read_state_locked()
            return copy.deepcopy([sample for sample in self._history[arm] if sample["seq"] > seq])

    def execute(self, action, mode="qpos"):
        return self.execute_arms(arm_targets(validate_qpos(action), mode))

    def execute_arms(self, targets):
        targets = validate_arm_targets(targets)
        with self._resources_lock:
            self._check_ready()
            sent = []
            for side, target in targets.items():
                # Distinct message objects avoid mutation after left publish.
                message = self._bindings.command_type()
                message.mode = 5 if target["mode"] == "qpos" else 4
                setattr(message, "joint_pos" if message.mode == 5 else "end_pos", target["values"][:6])
                message.gripper = target["values"][6]
                if hasattr(message, "header"):
                    message.header.stamp = self._node.get_clock().now().to_msg()
                try:
                    self._publishers[side].publish(message)
                    sent.append(side)
                except Exception as exc:
                    raise RuntimeError(f"publish failed for {side}; already published arms={sent}: {exc}") from exc

    def close(self):
        # Set cancellation before waiting on resource locks so connect's wait
        # can terminate immediately, including another thread's failed connect.
        self._stop.set()
        self.closed = True
        self._connected = False
        deadline = time.monotonic() + self._close_timeout
        with self._condition:
            self._condition.notify_all()
        if not self._close_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise RuntimeError("robot close exceeded bounded deadline: concurrent cleanup is busy")
        try:
            if self._cleanup_done:
                return
            if not self._resources_lock.acquire(timeout=max(0, deadline - time.monotonic())):
                raise RuntimeError("robot close exceeded bounded deadline: ROS initialization is busy")
            try:
                if self._executor is not None:
                    self._executor.wake()
            finally:
                self._resources_lock.release()
            for thread in (self._spin_thread, self._decoder_thread):
                if thread is not None and thread is not threading.current_thread():
                    thread.join(max(0, deadline - time.monotonic()))
            failures = []
            if not self._resources_lock.acquire(timeout=max(0, deadline - time.monotonic())):
                raise RuntimeError("robot close exceeded bounded deadline: ROS resources are busy")
            try:
                if self._executor is not None:
                    if not self._executor.shutdown(timeout_sec=max(0, deadline - time.monotonic())):
                        failures.append("ROS executor did not stop")
                if self._spin_thread is not None and self._spin_thread.is_alive():
                    failures.append("ROS thread did not stop")
                elif self._node is not None:
                    self._node.destroy_node()
                if self._context is not None:
                    self._context.try_shutdown()
                if self._decoder_thread is not None and self._decoder_thread.is_alive():
                    failures.append("image decoder thread did not stop")
                self._publishers.clear()
            finally:
                self._resources_lock.release()
            with self._condition:
                self._pending_images.clear()
                self._images.clear()
            if failures:
                raise RuntimeError("robot close exceeded bounded deadline: " + "; ".join(failures))
            self._cleanup_done = True
        finally:
            self._close_lock.release()
