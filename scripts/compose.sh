#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "${SETUP_PYTHON:-python3}" "$root/scripts/compose_project.py" "$@"
