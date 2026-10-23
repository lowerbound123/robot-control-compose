"""Robot backends. Importing this package never initializes or imports ROS."""
from .acone import ACOneRobot
from .mock import MockRobot


def make_robot(config: dict):
    robot_config = config["robot"]
    backend = robot_config.get("backend", "mock")
    if backend == "mock":
        return MockRobot(robot_config)
    if backend == "acone":
        return ACOneRobot(robot_config)
    raise ValueError(f"unknown robot backend: {backend!r}")


__all__ = ["make_robot", "MockRobot", "ACOneRobot"]
