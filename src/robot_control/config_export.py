"""Lifecycle-thread persistence of a policy's cached configuration export."""
from contextlib import nullcontext
import json
import os
from pathlib import Path
import stat
import tempfile

from rc_policy_sdk.config_export import validate_export
from .yaml_config import render_yaml_config


def save_export(directory, manifest, exported, *, guard=None):
    exported = validate_export(exported)
    root, manifest = Path(directory).resolve(), Path(manifest)
    destinations = [(manifest, None)]
    destinations.extend((root / item['path'], item['data']) for item in exported['files'])
    if len({path.resolve() for path, _ in destinations}) != len(destinations):
        raise ValueError('duplicate config export destination')
    for path, _ in destinations:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('config export destination must stay inside its policy source')
        if not path.parent.is_dir():
            raise ValueError('config export parent directory does not exist')
    payloads = [(manifest, render_yaml_config(manifest, exported['policy']))]
    payloads.extend((path, (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)+'\n').encode())
                    for path, data in destinations[1:])
    staged, backups, replaced = {}, {}, []
    def stage(path, data):
        fd, name = tempfile.mkstemp(prefix='.'+path.name+'.', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                if path.exists(): os.fchmod(stream.fileno(), stat.S_IMODE(path.stat().st_mode))
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            os.unlink(name)
            raise
        return name
    try:
        for path, data in payloads:
            staged[path] = stage(path, data)
            backups[path] = stage(path, path.read_bytes()) if path.exists() else None
        with guard() if guard else nullcontext():
            try:
                for path, _ in payloads:
                    os.replace(staged[path], path)
                    replaced.append(path)
            except BaseException as error:
                rollback_errors = []
                for path in reversed(replaced):
                    try:
                        if backups[path] is None: path.unlink()
                        else: os.replace(backups[path], path)
                    except OSError as exc:
                        rollback_errors.append(f'{path}: {exc}')
                if rollback_errors:
                    raise OSError(f'{error}; rollback failed: {"; ".join(rollback_errors)}') from error
                raise
        return [str(path) for path, _ in payloads]
    finally:
        for name in (*staged.values(), *backups.values()):
            if name and os.path.exists(name): os.unlink(name)
