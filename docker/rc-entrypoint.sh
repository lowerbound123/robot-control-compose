#!/bin/bash
set -e
python3 /workspace/packages/policy-sdk/src/rc_policy_sdk/source.py --check
exec "$@"
