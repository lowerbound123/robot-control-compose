FROM ros:humble-ros-base-jammy@sha256:566b87e6dfc48392189a9c32e594e84060fa9948de93b04e536f39475a1f6963 AS dependencies
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip python3-dev python3-colcon-common-extensions build-essential \
    ros-humble-sensor-msgs ros-humble-geometry-msgs ros-humble-std-msgs \
    ros-humble-rosidl-default-generators && rm -rf /var/lib/apt/lists/*
COPY connector/acone/requirements.lock /tmp/requirements.lock
RUN python3 -m pip install --no-cache-dir -r /tmp/requirements.lock && python3 -m pip check
FROM dependencies AS ros-build
COPY ros_ws/src/arx5_arm_msg /opt/rc_ros_ws/src/arx5_arm_msg
COPY ros_ws/src/robot_control_test_msgs /opt/rc_ros_ws/src/robot_control_test_msgs
RUN . /opt/ros/humble/setup.sh && cd /opt/rc_ros_ws && \
    colcon build --merge-install --packages-select arx5_arm_msg robot_control_test_msgs && \
    chmod -R a+rX /opt/rc_ros_ws/install
FROM dependencies AS runtime
COPY --from=ros-build /opt/rc_ros_ws/install /opt/rc_ros_ws/install
COPY connector/acone/entrypoint.sh /connector-entrypoint.sh
WORKDIR /workspace
ENTRYPOINT ["/bin/bash", "/connector-entrypoint.sh"]
FROM runtime AS test
COPY requirements/rc.lock /tmp/rc.lock
COPY requirements/test.lock /tmp/test.lock
RUN python3 -m pip install --no-cache-dir -r /tmp/rc.lock -r /tmp/test.lock
