#!/usr/bin/env python3
"""Rename private gripper settings offline, preserving values and backing up writes."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from robot_control.yaml_config import load_yaml_config, render_yaml_config, strict_equal
from config_files import GRIPPER_SOURCES as SOURCES, migrate_gripper_config as migrate_config, write_updates


def prepare_updates(root):
    root = Path(root).resolve()
    changes = []
    for source in SOURCES:
        path = root / 'policy' / source / 'policy.yaml'
        if (path.is_symlink() or any(parent.is_symlink() for parent in path.parents
                                     if parent.is_relative_to(root))):
            raise ValueError(f'{path.relative_to(root)}: symlink configuration is not supported')
        if not path.exists():
            continue
        current = load_yaml_config(path)
        try:
            migrated = migrate_config(current)
        except ValueError as exc:
            raise ValueError(f'{path.relative_to(root)}: {exc}') from None
        if not strict_equal(current, migrated):
            changes.append((path, render_yaml_config(path, migrated)))
    return changes


def main(argv=None, *, root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='back up and write changes (default: dry run)')
    args = parser.parse_args(argv)
    root = Path(root or ROOT).resolve()
    try:
        changes = prepare_updates(root)
        if args.apply:
            write_updates(root, changes)
    except (ValueError, OSError) as exc:
        print(f'Gripper configuration migration failed: {exc}', file=sys.stderr)
        return 1
    for path, _ in changes:
        print(f'{"Migrated" if args.apply else "Would migrate"}: {path.relative_to(root)}')
    if not changes:
        print('No gripper parameter names need migration.')
    elif args.apply:
        print('Original files backed up under .setup/backups/. Reload policy instances to use the new code.')
    else:
        print('Dry run: no files changed. Use --apply to back up and write these changes.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
