#!/bin/bash
set -e
source /opt/ros/humble/setup.bash
source /opt/rc_ros_ws/install/setup.bash
exec "$@"
