#!/bin/bash
set -e
source /opt/ros/humble/setup.bash
source /opt/rc_ros_ws/install/setup.bash
python3 /workspace/packages/policy-sdk/src/rc_policy_sdk/source.py --check
exec "$@"
