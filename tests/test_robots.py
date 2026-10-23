"""Adapter tests with a deliberately small ROS transport, not hardware evidence."""
import json
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from robot_control.robots import ACOneRobot, MockRobot, make_robot


def wait_until(check, timeout=1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.005)
    raise AssertionError("condition did not become true")


class ArmState:
    def __init__(self, offset=0):
        self.joint_pos = [offset + i for i in range(7)]
        self.joint_vel = [offset + i / 10 for i in range(7)]
        self.joint_cur = [offset + i / 100 for i in range(7)]
        self.end_pos = [offset + i / 1000 for i in range(6)]
        self.diagnostic = {"ready": True, "temperatures": [11, 12]}

    def get_fields_and_field_types(self):
        return dict.fromkeys(self.__dict__, "test")


class ArmCommand:
    def __init__(self):
        self.mode = 0
        self.joint_pos = [0.0] * 6
        self.end_pos = [0.0] * 6
        self.gripper = 0.0


class FakeContext:
    def __init__(self):
        self.running = False

    def ok(self):
        return self.running

    def try_shutdown(self):
        self.running = False


class FakeNode:
    def __init__(self):
        self.callbacks = {}
        self.published = {}
        self.destroyed = False

    def create_subscription(self, msg_type, topic, callback, qos):
        self.callbacks[topic] = callback
        return callback

    def create_publisher(self, msg_type, topic, qos):
        self.published[topic] = []
        return SimpleNamespace(publish=self.published[topic].append)

    def destroy_node(self):
        self.destroyed = True


class FakeExecutor:
    def __init__(self, context):
        self.context = context
        self.shutdown_called = False

    def add_node(self, node):
        self.node = node

    def spin_once(self, timeout_sec):
        time.sleep(min(timeout_sec, 0.005))

    def wake(self):
        pass

    def shutdown(self, timeout_sec):
        self.shutdown_called = True
        return True


@pytest.fixture
def fake_ros(monkeypatch):
    from robot_control.robots import acone
    node = FakeNode()
    binding = SimpleNamespace(
        rclpy=SimpleNamespace(
            init=lambda **kwargs: setattr(kwargs["context"], "running", True),
            create_node=lambda *args, **kwargs: node,
        ),
        Context=FakeContext,
        Executor=FakeExecutor,
        message_type=ArmState,
        command_type=ArmCommand,
        CompressedImage=object,
        sensor_qos=object(),
        signal_options=0,
    )
    monkeypatch.setattr(acone, "_load_ros", lambda message_type: binding)
    return node


def connect_fake(robot, node):
    result = []
    thread = threading.Thread(target=lambda: _capture_connect(robot, result))
    thread.start()
    wait_until(lambda: "left" in node.callbacks and "right" in node.callbacks)
    node.callbacks["left"](ArmState(1))
    node.callbacks["right"](ArmState(10))
    thread.join(1)
    assert not thread.is_alive()
    assert result == [None]


def _capture_connect(robot, result, cancel_event=None):
    try:
        robot.connect(cancel_event=cancel_event)
        result.append(None)
    except Exception as error:
        result.append(error)


def robot_config(**patch):
    return {
        "feedback_left_topic": "left", "feedback_right_topic": "right",
        "command_left_topic": "command_left", "command_right_topic": "command_right",
        "camera_topics": {}, "connect_timeout_s": 0.6,
        **patch,
    }


def test_mock_has_no_ros_import_and_factory_validates_backend():
    completed = subprocess.run(
        [sys.executable, "-c", "import sys; from robot_control.robots import make_robot; "
         "r=make_robot({'robot': {'backend': 'mock'}}); r.connect(); "
         "assert not any(k == 'rclpy' or k.startswith('rclpy.') for k in sys.modules); r.close()"],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    with pytest.raises(ValueError, match="backend"):
        make_robot({"robot": {"backend": "unknown"}})
    assert isinstance(make_robot({"robot": {"backend": "acone"}}), ACOneRobot)


def test_mock_feedback_follows_commands_and_returns_json_state():
    robot = MockRobot()
    with pytest.raises(RuntimeError, match="connect"):
        robot.read_state()
    robot.connect()
    target = np.linspace(-1, 1, 14)
    robot.execute(target)
    target[:] = 99
    state = robot.read_state()
    assert state["qpos"] == pytest.approx(np.linspace(-1, 1, 14))
    assert set(("qpos", "qvel", "effort", "eef", "timestamp")) <= state.keys()
    json.dumps(state, allow_nan=False)
    images = robot.read_images()
    assert set(images) == {"head", "left_wrist", "right_wrist"}
    assert all(image.shape[-1] == 3 and image.dtype == np.uint8 for image, _ in images.values())
    assert len(robot.commands) == 1
    robot.close()
    robot.close()
    assert robot.closed
    with pytest.raises(RuntimeError, match="closed"):
        robot.execute(np.zeros(14))


@pytest.mark.parametrize("target", [np.zeros(13), np.zeros((2, 7)), np.full(14, np.nan)])
def test_invalid_action_never_published(target, fake_ros):
    robot = ACOneRobot(robot_config())
    connect_fake(robot, fake_ros)
    try:
        with pytest.raises(ValueError):
            robot.execute(target)
        assert not any(fake_ros.published.values())
    finally:
        robot.close()


def test_acone_raw_state_eef_and_separate_publish_objects(fake_ros):
    robot = ACOneRobot(robot_config())
    connect_fake(robot, fake_ros)
    try:
        state = robot.read_state()
        assert state["qpos"] == list(ArmState(1).joint_pos) + list(ArmState(10).joint_pos)
        assert state["eef"] == list(ArmState(1).end_pos) + [7] + list(ArmState(10).end_pos) + [16]
        assert state["raw"]["left"]["diagnostic"] == {"ready": True, "temperatures": [11, 12]}
        json.dumps(state, allow_nan=False)
        state["raw"]["left"]["joint_pos"][0] = 999
        assert robot.read_state()["raw"]["left"]["joint_pos"][0] == 1
        robot.execute(np.arange(14, dtype=float))
        left = fake_ros.published["command_left"][0]
        right = fake_ros.published["command_right"][0]
        assert left is not right
        assert left.joint_pos == list(range(6)) and left.gripper == 6
        assert right.joint_pos == list(range(7, 13)) and right.gripper == 13
        robot.execute(np.ones(14))
        assert left.joint_pos == list(range(6))
    finally:
        robot.close()
    assert fake_ros.destroyed
    assert not robot._spin_thread.is_alive()
    assert not robot._decoder_thread.is_alive()
    robot.close()


def test_acone_missing_and_stale_feedback_and_cancellable_connect(fake_ros):
    robot = ACOneRobot(robot_config(feedback_timeout_s=0.03))
    connect_fake(robot, fake_ros)
    try:
        time.sleep(0.04)
        with pytest.raises(RuntimeError, match="stale.*feedback"):
            robot.read_state()
    finally:
        robot.close()
    robot = ACOneRobot(robot_config(connect_timeout_s=5))
    result = []
    thread = threading.Thread(target=lambda: _capture_connect(robot, result))
    thread.start()
    wait_until(lambda: robot._spin_thread is not None)
    start = time.monotonic()
    robot.close()
    thread.join(0.5)
    assert time.monotonic() - start < 0.5
    assert not thread.is_alive()
    assert isinstance(result[0], RuntimeError)


def test_failed_connect_cleans_up(fake_ros):
    robot = ACOneRobot(robot_config(connect_timeout_s=0.02))
    with pytest.raises(RuntimeError, match="feedback"):
        robot.connect()
    assert fake_ros.destroyed
    assert not robot._spin_thread.is_alive()
    robot.close()


def test_event_cancel_while_waiting_for_feedback(fake_ros):
    robot = ACOneRobot(robot_config(connect_timeout_s=5))
    cancel = threading.Event()
    result = []
    thread = threading.Thread(target=lambda: _capture_connect(robot, result, cancel))
    thread.start()
    wait_until(lambda: robot._spin_thread is not None)
    started = time.monotonic()
    cancel.set()
    thread.join(0.5)
    assert not thread.is_alive()
    assert time.monotonic() - started < 0.5
    assert isinstance(result[0], RuntimeError) and "cancelled" in str(result[0])
    assert robot.closed and robot._cleanup_done
    assert fake_ros.destroyed
    assert not robot._spin_thread.is_alive()
    assert not robot._decoder_thread.is_alive()


def test_event_cancel_before_ros_import_and_mock_connect(fake_ros, monkeypatch):
    from robot_control.robots import acone
    cancel = threading.Event()
    cancel.set()

    def unexpected_loader(message_type):
        raise AssertionError("cancelled connection must not import ROS")

    monkeypatch.setattr(acone, "_load_ros", unexpected_loader)
    robot = ACOneRobot(robot_config())
    with pytest.raises(RuntimeError, match="cancelled"):
        robot.connect(cancel_event=cancel)
    assert robot.closed and robot._cleanup_done
    mock = MockRobot()
    with pytest.raises(RuntimeError, match="cancelled"):
        mock.connect(cancel_event=cancel)
    mock.close()


def test_event_cancel_after_native_init_cleans_context(fake_ros, monkeypatch):
    from robot_control.robots import acone
    original_loader = acone._load_ros
    cancel = threading.Event()

    def load_binding(message_type):
        binding = original_loader(message_type)

        def native_init(**kwargs):
            kwargs["context"].running = True
            cancel.set()

        binding.rclpy.init = native_init
        return binding

    monkeypatch.setattr(acone, "_load_ros", load_binding)
    robot = ACOneRobot(robot_config())
    with pytest.raises(RuntimeError, match="cancelled"):
        robot.connect(cancel_event=cancel)
    assert robot.closed and robot._cleanup_done
    assert not robot._context.ok()
    assert robot._node is None


def test_close_deadline_during_ros_initialization(fake_ros, monkeypatch):
    from robot_control.robots import acone
    original_loader = acone._load_ros
    entered = threading.Event()
    unblock = threading.Event()

    def load_binding(message_type):
        binding = original_loader(message_type)

        def blocking_init(**kwargs):
            entered.set()
            unblock.wait(1)
            kwargs["context"].running = True

        binding.rclpy.init = blocking_init
        return binding

    monkeypatch.setattr(acone, "_load_ros", load_binding)
    robot = ACOneRobot(robot_config(close_timeout_s=0.03))
    result = []
    thread = threading.Thread(target=lambda: _capture_connect(robot, result))
    thread.start()
    assert entered.wait(0.5)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="bounded deadline.*initialization"):
        robot.close()
    assert time.monotonic() - started < 0.2
    unblock.set()
    thread.join(0.5)
    assert not thread.is_alive()
    assert isinstance(result[0], RuntimeError)
    assert not robot._context.ok()
    assert robot._cleanup_done
    robot.close()


def test_malformed_feedback_invalidates_previous_sample(fake_ros):
    robot = ACOneRobot(robot_config())
    connect_fake(robot, fake_ros)
    try:
        message = ArmState(2)
        message.joint_pos = [0] * 6
        fake_ros.callbacks["left"](message)
        with pytest.raises(RuntimeError, match="missing left arm feedback.*joint_pos"):
            robot.read_state()
        fake_ros.callbacks["left"](ArmState(3))
        assert robot.read_state()["qpos"][0] == 3
    finally:
        robot.close()


def test_compressed_decode_rgb_latest_queue_and_staleness(fake_ros, monkeypatch):
    from io import BytesIO
    from PIL import Image
    from robot_control.robots import acone
    red = np.zeros((3, 5, 3), dtype=np.uint8)
    red[:] = [220, 20, 10]
    encoded = BytesIO()
    Image.fromarray(red).save(encoded, format="PNG")
    original_decode = acone._decode_rgb
    blocked = threading.Event()
    entered = threading.Event()

    def slow_decode(data):
        entered.set()
        blocked.wait(0.5)
        return original_decode(data)

    monkeypatch.setattr(acone, "_decode_rgb", slow_decode)
    robot = ACOneRobot(robot_config(camera_topics={"head": "camera"}, camera_timeout_s=0.08))
    result = []
    thread = threading.Thread(target=lambda: _capture_connect(robot, result))
    thread.start()
    wait_until(lambda: "camera" in fake_ros.callbacks)
    fake_ros.callbacks["left"](ArmState(1))
    fake_ros.callbacks["right"](ArmState(10))
    stamp = SimpleNamespace(sec=123, nanosec=500_000_000)
    message = SimpleNamespace(data=encoded.getvalue(), header=SimpleNamespace(stamp=stamp))
    fake_ros.callbacks["camera"](message)
    assert entered.wait(0.5)
    for _ in range(50):
        fake_ros.callbacks["camera"](message)
    assert len(robot._pending_images) <= 1
    blocked.set()
    thread.join(1)
    assert result == [None]
    try:
        wait_until(lambda: bool(robot.image_snapshot()[0]))
        images = robot.read_images()
        image, timestamp = images["head"]
        assert np.array_equal(image, red)
        assert timestamp == 123.5
        assert not image.flags.writeable
        time.sleep(0.09)
        with pytest.raises(RuntimeError, match="stale.*camera"):
            robot.read_images()
    finally:
        robot.close()


def test_bad_image_does_not_block_feedback_but_cannot_be_used_by_policy(fake_ros):
    robot = ACOneRobot(robot_config(camera_topics={"head": "camera"}, connect_timeout_s=0.08))
    result = []
    thread = threading.Thread(target=lambda: _capture_connect(robot, result))
    thread.start()
    wait_until(lambda: "camera" in fake_ros.callbacks)
    fake_ros.callbacks["left"](ArmState(1))
    fake_ros.callbacks["right"](ArmState(10))
    fake_ros.callbacks["camera"](SimpleNamespace(data=b"not an image"))
    thread.join(1)
    assert result == [None]
    with pytest.raises(RuntimeError, match="head"):
        robot.read_images()
    robot.close()
    assert not robot._decoder_thread.is_alive()
