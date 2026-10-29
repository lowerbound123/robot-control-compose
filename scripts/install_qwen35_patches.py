#!/usr/bin/env python3
"""Validate the mounted Qwen3.5 patch; never install, copy or modify files.

The historical command name and --check option remain usable. Both invocations
perform the same read-only validation against the current repository source.
"""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'packages/qwen35-policy/src'))
from rc_qwen35_policy.patches import check


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='read-only (also the default)')
    parser.parse_args()
    print(f'Qwen3.5 patch verified: {check()}')


if __name__ == '__main__':
    main()
