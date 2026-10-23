import time
import threading
from io import BytesIO
from types import SimpleNamespace

from PIL import Image
from test_robots import fake_ros as fake_ros, connect_fake, robot_config, _capture_connect
from test_controller import until


def compressed():
    stream = BytesIO()
    Image.new("RGB", (8, 6), "red").save(stream, format="PNG")
    return SimpleNamespace(data=stream.getvalue())


def test_camera_preview_preserves_receive_age_and_sequence_without_arm_feedback(fake_ros):
    from robot_control.robots import ACOneRobot
    robot = ACOneRobot(robot_config(camera_topics={"head": "camera"}))
    connect_fake(robot, fake_ros)
    try:
        assert robot.camera_preview_snapshot() == {}
        robot._on_image("head", compressed())
        until(lambda: bool(robot.camera_preview_snapshot()))
        first = robot.camera_preview_snapshot()["head"]
        assert first["sequence"] == 1
        assert time.monotonic() - first["received"] < 1
        with robot._condition:
            robot._feedback.clear()
        second = robot.camera_preview_snapshot()["head"]
        assert second["sequence"] == first["sequence"]
        assert second["received"] == first["received"]
        assert second["image"] is first["image"]
        # Even identical pixels (and repeated ROS timestamps) are a new frame.
        robot._on_image("head", compressed())
        until(lambda: robot.camera_preview_snapshot()["head"]["sequence"] == 2)
        robot._on_image("head", SimpleNamespace(data=b"invalid image"))
        until(lambda: bool(robot._image_errors))
        assert robot.camera_preview_snapshot()["head"]["sequence"] == 2
    finally:
        robot.close()


def test_camera_preview_works_while_initial_arm_feedback_is_missing(fake_ros):
    from robot_control.robots import ACOneRobot
    robot = ACOneRobot(robot_config(camera_topics={"head": "camera"}, connect_timeout_s=5))
    result = []
    thread = threading.Thread(target=lambda: _capture_connect(robot, result))
    thread.start()
    try:
        until(lambda: "camera" in fake_ros.callbacks)
        fake_ros.callbacks["camera"](compressed())
        until(lambda: bool(robot._images))
        assert not robot._connected and thread.is_alive()
        assert robot.camera_preview_snapshot()["head"]["sequence"] == 1
    finally:
        robot.close()
        thread.join(1)
    assert not thread.is_alive()
