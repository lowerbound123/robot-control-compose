"""Synthetic two-arm closed-loop node with three PNG-compressed RGB cameras."""
import argparse
import io
import math

import numpy as np
from PIL import Image
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from robot_control_test_msgs.msg import ArmState, ArmCommand
from sensor_msgs.msg import CompressedImage


class ArmCameraSimulator(Node):
    def __init__(self, namespace="/robot_control_test", frequency_hz=30):
        super().__init__("robot_control_test_simulator", namespace=namespace)
        self.frequency_hz = frequency_hz
        self.positions = {"left": np.zeros(7), "right": np.zeros(7)}
        self.targets = {side: values.copy() for side, values in self.positions.items()}
        self.end_positions = {side: np.zeros(6) for side in self.positions}
        self.end_targets = {side: np.zeros(6) for side in self.positions}
        self.modes = {side: 5 for side in self.positions}
        self.command_counts = {"left": 0, "right": 0}
        self.feedback_publishers = {}
        for side in ("left", "right"):
            self.feedback_publishers[side] = self.create_publisher(ArmState, f"feedback/{side}", qos_profile_sensor_data)
            self.create_subscription(
                ArmCommand, f"command/{side}", lambda message, side=side: self.command(side, message), 10,
            )
        self.camera_publishers = {}
        self.compressed_frames = {}
        for index, camera in enumerate(("head", "left_wrist", "right_wrist")):
            self.camera_publishers[camera] = self.create_publisher(
                CompressedImage, f"camera/{camera}/compressed", qos_profile_sensor_data,
            )
            pixels = np.zeros((48, 64, 3), dtype=np.uint8)
            pixels[..., index] = 220
            pixels[..., (index + 1) % 3] = np.arange(64, dtype=np.uint8)
            buffer = io.BytesIO()
            Image.fromarray(pixels).save(buffer, format="PNG")
            self.compressed_frames[camera] = buffer.getvalue()
        self.tick_count = 0
        self.create_timer(1.0 / frequency_hz, self.tick)

    def command(self, side, message):
        target = np.asarray(message.joint_pos if message.mode == 5 else message.end_pos, dtype=float)
        if message.mode not in (4, 5) or target.shape != (6,) or not np.all(np.isfinite(target)) or not math.isfinite(message.gripper):
            self.get_logger().error(f"invalid {side} RobotCmd command")
            return
        self.modes[side] = message.mode
        if message.mode == 5:
            self.targets[side][:6] = target
        else:
            self.end_targets[side] = target.copy()
        self.targets[side][6] = message.gripper
        self.command_counts[side] += 1

    def tick(self):
        for side in ("left", "right"):
            delta = np.clip(self.targets[side] - self.positions[side], -0.02, 0.02)
            self.positions[side] += delta
            if self.modes[side] == 4:
                self.end_positions[side] += np.clip(self.end_targets[side] - self.end_positions[side], -.02, .02)
            message = ArmState()
            message.joint_pos = self.positions[side].tolist()
            message.joint_vel = (delta * self.frequency_hz).tolist()
            message.joint_cur = (np.abs(delta) * 0.1).tolist()
            # Synthetic values, deliberately no claim of physical kinematics.
            message.end_pos = self.end_positions[side].tolist()
            self.feedback_publishers[side].publish(message)
        if self.tick_count % 3 == 0:
            stamp = self.get_clock().now().to_msg()
            for camera, publisher in self.camera_publishers.items():
                message = CompressedImage()
                message.header.stamp = stamp
                message.header.frame_id = camera
                message.format = "rgb8; png compressed rgb8"
                message.data = self.compressed_frames[camera]
                publisher.publish(message)
        self.tick_count += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="/robot_control_test")
    parser.add_argument("--frequency-hz", type=float, default=30)
    options, ros_args = parser.parse_known_args()
    if not math.isfinite(options.frequency_hz) or options.frequency_hz <= 0:
        parser.error("frequency-hz must be finite and positive")
    rclpy.init(args=ros_args)
    node = ArmCameraSimulator(options.namespace, options.frequency_hz)
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
