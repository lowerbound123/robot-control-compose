"""Validate mounted Python sources before starting a container's application.

This file is also an absolute-path entrypoint: a missing SDK mount must never
fall back to an installed SDK. Only the standard library is used here.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import runpy
import sys


def validate_sources(expected: dict[str, str]) -> dict:
    if not isinstance(expected, dict) or not expected:
        raise RuntimeError('source declarations must be a nonempty object')
    report = {}
    for name, directory in expected.items():
        if not isinstance(name, str) or not name.isidentifier() or not isinstance(directory, str):
            raise RuntimeError('source declarations require top-level module names and absolute paths')
        root = Path(directory)
        if not root.is_absolute() or not root.is_dir():
            raise RuntimeError(f'{name}: source directory is missing: {root}')
        root = root.resolve()
        candidates = (root / name / '__init__.py', root / (name + '.py'))
        spec = importlib.util.find_spec(name)
        if spec is None or spec.origin is None:
            raise RuntimeError(f'{name}: source module is unavailable in {root}')
        origin = Path(spec.origin).resolve()
        if origin not in candidates or not origin.is_relative_to(root):
            raise RuntimeError(f'{name}: incorrect source origin {origin}; expected {root}')
        try:
            digest = hashlib.sha256(origin.read_bytes()).hexdigest()
        except OSError as exc:
            raise RuntimeError(f'{name}: source is unreadable: {origin}') from exc
        report[name] = {'path': str(origin), 'sha256': digest}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--module')
    args, rest = parser.parse_known_args()
    try:
        expected = json.loads(os.environ.get('RC_SOURCE_MODULES', '{}'))
        report = validate_sources(expected)
    except (RuntimeError, ValueError) as exc:
        parser.exit(1, f'source validation failed: {exc}\n')
    if args.check:
        if rest:
            parser.error('unexpected check arguments')
        print(json.dumps(report, sort_keys=True))
    else:
        sys.argv = [args.module, *rest]
        runpy.run_module(args.module, run_name='__main__', alter_sys=True)


if __name__ == '__main__':
    main()
