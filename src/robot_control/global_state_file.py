"""Strict data-only env format and deadline-bounded atomic persistence."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from rc_policy_sdk.global_state import decode, encode, validate_key


def read_state(path, capacity, *, cancel_event=None, deadline=None):
    def check():
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError('global state load cancelled')
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError('global state load deadline exceeded')
    check()
    path = Path(path)
    if path.is_symlink():
        raise ValueError('global state file must not be a symlink')
    if not path.exists():
        return {}
    if not path.is_file():
        raise ValueError('global state must be a regular file')
    result = {}
    size = 2  # braces; count each encoded entry once, not every prior entry.
    with path.open(encoding='utf-8') as stream:
        for number, line in enumerate(stream, 1):
            check()
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            try:
                key, raw = line.strip().split('=', 1)
                validate_key(key)
                if key in result:
                    raise ValueError('duplicate key')
                value = decode(raw)
                entry = encode({key: value}, capacity)
                size += len(entry) - 2 + bool(result)
                if size > capacity:
                    raise ValueError('global state capacity exceeded')
                result[key] = value
            except (TypeError, ValueError, RecursionError) as exc:
                raise ValueError(f'invalid global.env at line {number}: {type(exc).__name__}') from None
    check()
    return result


def _write(path, values):
    import json
    path = Path(path)
    if path.is_symlink():
        raise ValueError('global state file must not be a symlink')
    raw = ''.join(key + '=' + json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')) + '\n'
                  for key, value in sorted(values.items()))
    descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_state(path, values, deadline):
    raw = encode(values)
    if deadline - time.monotonic() <= .1:
        raise TimeoutError('global save deadline exceeded')
    # Separate process: a stuck filesystem must not strand an RC Python thread.
    process = subprocess.Popen([sys.executable, '-m', 'robot_control.global_state_file', str(path)],
                               stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        _, error = process.communicate(raw, timeout=max(.001, deadline - time.monotonic() - .1))
        if process.returncode:
            raise RuntimeError('global save failed: ' + error.decode('utf-8', 'replace')[-1024:])
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=max(.001, deadline - time.monotonic()))
        raise


if __name__ == '__main__':
    values = decode(sys.stdin.buffer.read())
    encode(values)
    _write(sys.argv[1], values)
