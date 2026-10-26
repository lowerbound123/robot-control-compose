FROM ros:humble-ros-base-jammy@sha256:566b87e6dfc48392189a9c32e594e84060fa9948de93b04e536f39475a1f6963 AS runtime
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-pip python3-dev python3-colcon-common-extensions build-essential \
    ros-humble-sensor-msgs ros-humble-geometry-msgs ros-humble-std-msgs \
    ros-humble-rosidl-default-generators && rm -rf /var/lib/apt/lists/*
COPY requirements/rc.lock /tmp/rc.lock
RUN python3 -m pip install --no-cache-dir -r /tmp/rc.lock
WORKDIR /opt/app
COPY packages/policy-sdk packages/policy-sdk
COPY pyproject.toml README.md ./
COPY src src
RUN python3 -m pip install --no-cache-dir --upgrade pip==25.3 setuptools==75.8.2 wheel==0.45.1 packaging==26.3 && \
    python3 -m pip install --no-cache-dir --no-build-isolation --no-deps ./packages/policy-sdk .
COPY ros_ws/src /opt/rc_ros_ws/src
RUN . /opt/ros/humble/setup.sh && cd /opt/rc_ros_ws && \
    colcon build --merge-install --packages-select arx5_arm_msg robot_control_test_msgs robot_control_test_sim
COPY configs configs
COPY rc.json ./
COPY policy policy
COPY docker/rc-entrypoint.sh /usr/local/bin/rc-entrypoint
RUN chmod +x /usr/local/bin/rc-entrypoint
WORKDIR /data
ENTRYPOINT ["/usr/local/bin/rc-entrypoint"]
CMD ["python3", "-m", "robot_control", "--config", "/workspace/configs/rc.mock.json"]

FROM runtime AS test
COPY requirements/test.lock /tmp/test.lock
RUN python3 -m pip install --no-cache-dir -r /tmp/test.lock
COPY tests /opt/app/tests
COPY packages/qwen35-policy /opt/app/packages/qwen35-policy
COPY packages/llm-direction-policy /opt/app/packages/llm-direction-policy
COPY vendor/qwen35 /opt/app/vendor/qwen35
COPY scripts /opt/app/scripts
WORKDIR /opt/app
CMD ["python3", "-m", "pytest", "tests/test_docker_integration.py", "-q", "-rs"]
