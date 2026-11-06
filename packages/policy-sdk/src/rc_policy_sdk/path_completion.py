"""Bounded name-only directory advice, shared by RC and the policy host.

Call from background work: an OS directory operation itself cannot be interrupted.
The host isolates this function from its reader, watchdog and lifecycle threads.
"""
import math
import os
from pathlib import Path
import time

from .argument_hints import normalize_parameter

MAX_RESULTS = 200
MAX_SCAN = 4096


def path_error(message):
    text = ''.join(f'\\x{ord(char):02x}' if ord(char) < 32 or ord(char) == 127 else char for char in str(message)[:512])
    return {'items': [], 'truncated': False, 'error': text[:512]}


def _has_controls(text):
    return any(ord(char) < 32 or ord(char) == 127 for char in text)


def _within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def list_paths(spec, prefix, base_dir, *, limit=200, timeout=1):
    try:
        if type(prefix) is not str or len(prefix) > 4096 or _has_controls(prefix):
            raise ValueError('path prefix must be a bounded string')
        if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
            raise ValueError('path result limit must be 1 to 200')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('path completion timed out')
        deadline = time.monotonic() + min(timeout, 1.)
        parameter = normalize_parameter(spec)
        if parameter['type'] != 'path':
            raise ValueError('not a path parameter')
        declared = os.path.normpath(parameter['root'])
        if _has_controls(declared):
            raise ValueError('declared root contains unsupported terminal controls')
        absolute = os.path.isabs(declared)
        base = Path(base_dir)
        root = (Path(declared) if absolute else base / declared).resolve(strict=True)
        if not root.is_dir():
            raise ValueError('declared path root is not a directory')
        if time.monotonic() >= deadline:
            raise ValueError('path completion timed out')
        if prefix and prefix != declared and declared.startswith(prefix):
            return {'items': [{'name': Path(declared).name or '/', 'kind': 'directory',
                               'text': declared.rstrip('/') + '/'}], 'truncated': False, 'error': None}
        if not prefix or prefix == declared:
            text = declared.rstrip('/') + '/'
        elif os.path.isabs(prefix) or declared == '.' or prefix.startswith(declared + '/'):
            text = prefix
        else:
            text = declared.rstrip('/') + '/' + prefix
        parent_text, partial = os.path.split(text)
        directory = (Path(parent_text) if os.path.isabs(parent_text) else base / parent_text).resolve(strict=True)
        if not _within(directory, root):
            raise ValueError('path prefix escapes the declared root')
        items, scanned, truncated = [], 0, False
        with os.scandir(directory) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    return {'items': [], 'truncated': True, 'error': 'path completion timed out'}
                scanned += 1
                if scanned > MAX_SCAN:
                    truncated = True
                    break
                name = entry.name
                if _has_controls(name):
                    continue
                if not name.startswith(partial) or (name.startswith('.') and not partial.startswith('.')):
                    continue
                try:
                    resolved = (directory / name).resolve(strict=True)
                    if not _within(resolved, root):
                        continue
                    is_dir = entry.is_dir()
                    if not is_dir and (parameter['kind'] == 'directory' or not entry.is_file()):
                        continue
                except OSError:
                    continue
                if not is_dir and parameter['suffixes'] and not any(name.endswith(suffix) for suffix in parameter['suffixes']):
                    continue
                inserted = os.path.normpath(os.path.join(declared, str(directory.relative_to(root)), name)) + ('/' if is_dir else '')
                items.append({'name': name, 'kind': 'directory' if is_dir else 'file', 'text': inserted})
        if time.monotonic() >= deadline:
            return {'items': [], 'truncated': True, 'error': 'path completion timed out'}
        items.sort(key=lambda item: (item['kind'] != 'directory', item['name']))
        return {'items': items[:limit], 'truncated': truncated or len(items) > limit, 'error': None}
    except (OSError, ValueError, RuntimeError) as exc:
        return path_error(exc)
