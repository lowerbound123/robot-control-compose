#!/usr/bin/env python3
"""Standalone entry for the shared stdlib-only recording reduction implementation."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from robot_control.recording_videos import entrypoint


if __name__ == '__main__':
    raise SystemExit(entrypoint())
