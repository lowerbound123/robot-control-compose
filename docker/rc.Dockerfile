FROM docker:28.5.1-cli AS docker-cli
FROM docker/compose-bin:v2.39.1 AS compose-cli
FROM ros:humble-ros-base-jammy@sha256:566b87e6dfc48392189a9c32e594e84060fa9948de93b04e536f39475a1f6963 AS dependencies
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=compose-cli /docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip python3-dev python3-colcon-common-extensions build-essential \
    ros-humble-sensor-msgs ros-humble-geometry-msgs ros-humble-std-msgs \
    ros-humble-rosidl-default-generators && rm -rf /var/lib/apt/lists/*
COPY requirements/rc.lock /tmp/rc.lock
RUN python3 -m pip install --no-cache-dir -r /tmp/rc.lock && python3 -m pip check

FROM dependencies AS ros-build
COPY ros_ws/src/arx5_arm_msg /opt/rc_ros_ws/src/arx5_arm_msg
COPY ros_ws/src/robot_control_test_msgs /opt/rc_ros_ws/src/robot_control_test_msgs
RUN . /opt/ros/humble/setup.sh && cd /opt/rc_ros_ws && \
    colcon build --merge-install --packages-select arx5_arm_msg robot_control_test_msgs && \
    chmod -R a+rX /opt/rc_ros_ws/install

FROM dependencies AS runtime
COPY --from=ros-build /opt/rc_ros_ws/install /opt/rc_ros_ws/install
ENV PYTHONPYCACHEPREFIX=/tmp/rc-pycache
WORKDIR /workspace
ENTRYPOINT ["/bin/bash", "/workspace/docker/rc-entrypoint.sh"]
CMD ["python3", "-m", "robot_control", "--config", "/workspace/configs/rc.mock.yaml"]

FROM runtime AS test
COPY requirements/test.lock /tmp/test.lock
RUN python3 -m pip install --no-cache-dir -r /tmp/test.lock && python3 -m pip check
ENV PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
CMD ["python3", "-m", "pytest", "-q", "-p", "pytest_asyncio.plugin", "-p", "no:cacheprovider"]
