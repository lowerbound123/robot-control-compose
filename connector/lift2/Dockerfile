FROM ros:noetic-ros-core@sha256:fd6279f04e51fa61201a791da9a156129941887b95b614ae0cec7865ef49de4f AS ros-messages
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ros-noetic-message-generation ros-noetic-sensor-msgs && rm -rf /var/lib/apt/lists/*
COPY ros_ws/lift2/arm_control /opt/lift2_msgs/src/arm_control
RUN . /opt/ros/noetic/setup.sh && cd /opt/lift2_msgs && \
    catkin_make arm_control_generate_messages_py -DCATKIN_ENABLE_TESTING=OFF && \
    mkdir -p /opt/ros1-python && \
    for package in rospy roslib rosgraph genpy genmsg catkin roscpp rosgraph_msgs geometry_msgs std_msgs sensor_msgs; do \
      cp -a /opt/ros/noetic/lib/python3/dist-packages/$package /opt/ros1-python/; \
    done && \
    cp -a devel/lib/python3/dist-packages/arm_control /opt/ros1-python/ && \
    cp /opt/ros/noetic/etc/ros/python_logging.conf /opt/ros1-python/python_logging.conf && \
    find /opt/ros1-python -type d -name __pycache__ -exec rm -rf '{}' +

FROM python:3.10-slim-bookworm@sha256:5be5aaecb962a41567bc8041f1e9e3432cb085b037fe057f9a562630b2e4b370 AS wheels
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY connector/lift2/requirements.lock /tmp/requirements.lock
RUN python3 -m pip wheel --no-cache-dir -r /tmp/requirements.lock -w /wheels

FROM python:3.10-slim-bookworm@sha256:5be5aaecb962a41567bc8041f1e9e3432cb085b037fe057f9a562630b2e4b370 AS runtime
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 ROS_VERSION=1 ROS_DISTRO=noetic
ENV ROS_PYTHON_LOG_CONFIG_FILE=/opt/ros1-python/python_logging.conf
COPY --from=wheels /wheels /wheels
COPY connector/lift2/requirements.lock /tmp/requirements.lock
RUN python3 -m pip install --no-index --find-links=/wheels -r /tmp/requirements.lock && \
    python3 -m pip check && rm -rf /wheels
COPY --from=ros-messages /opt/ros1-python /opt/ros1-python
RUN python3 -c "import site,pathlib; pathlib.Path(site.getsitepackages()[0], 'ros1.pth').write_text('/opt/ros1-python\n')" && \
    python3 -c "import rospy, sensor_msgs.msg, arm_control.msg; assert arm_control.msg.PosCmd._md5sum == 'df5f2abbfa9e683f82c8a2751cdc09f0'"
WORKDIR /workspace

# Test master and simulator only; never used as a hardware connector runtime.
FROM ros-messages AS simulator
