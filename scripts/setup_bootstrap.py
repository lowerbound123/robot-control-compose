#!/usr/bin/env python3
"""Prepare setup-only dependencies in a private, owned virtual environment."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

VERSION = '0.19.1'
OWNER = 'robot-control-setup'


def _atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def prepare(root, *, runner=subprocess.run, identity=None):
    root = Path(root).resolve()
    identity = identity or {'executable':str(Path(sys.executable).resolve()), 'version':sys.version}
    lock = root/'requirements/setup.lock'
    fingerprint = dict(identity=identity, lock=hashlib.sha256(lock.read_bytes()).hexdigest())
    state = root/'.setup'
    env = state/'tools-venv'
    marker = env/'rc-setup-owner.json'
    metadata = state/'tools-venv.json'
    guard = state/'tools-venv.lock'
    for path in (state, env, marker, metadata, guard):
        if path.is_symlink():
            raise ValueError(f'{path}: symlink tool environment is not supported')
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with guard.open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            owned = json.loads(marker.read_text()) == {'owner':OWNER}
        except (OSError, ValueError):
            owned = False
        if env.exists() and not owned:
            raise ValueError(f'{env}: refusing to replace an environment not owned by setup')
        python = env/'bin/python'
        try:
            cached = json.loads(metadata.read_text())
        except (OSError, ValueError):
            cached = None
        if cached == fingerprint and python.is_file():
            result = runner([str(python), '-c', 'from importlib.metadata import version; print(version("ruamel.yaml"))'],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if result.returncode == 0 and result.stdout.strip() == VERSION:
                return python
        metadata.unlink(missing_ok=True)
        if env.exists():
            shutil.rmtree(env)
        env.mkdir(mode=0o700)
        _atomic_json(marker, {'owner':OWNER})
        runner([identity['executable'], '-m', 'venv', str(env)], check=True)
        runner([str(python), '-m', 'pip', 'install', '--disable-pip-version-check', '--require-hashes',
                '--only-binary=:all:', '-r', str(lock)], check=True)
        result = runner([str(python), '-c', 'from importlib.metadata import version; print(version("ruamel.yaml"))'],
                        check=True, text=True, stdout=subprocess.PIPE)
        if result.stdout.strip() != VERSION:
            raise ValueError('setup YAML dependency version mismatch')
        _atomic_json(metadata, fingerprint)
        return python


def main():
    if sys.version_info < (3, 9):
        raise SystemExit('setup requires Python 3.9+ with venv support; select it with SETUP_PYTHON')
    if os.getuid() == 0:
        raise SystemExit('run setup as the normal host user, not root')
    root = Path(__file__).resolve().parents[1]
    try:
        python = prepare(root)
    except (OSError, ValueError, subprocess.CalledProcessError):
        raise SystemExit('setup tool environment failed; verify Python venv support and dependency availability') from None
    os.execv(str(python), [str(python), str(root/'scripts/setup_project.py'), *sys.argv[1:]])


if __name__ == '__main__':
    main()
