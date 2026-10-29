"""Standard-library-only preparation of private local configuration files."""
from __future__ import annotations

import copy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
import uuid


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def read_json(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f'{path}: symlink configuration is not supported')
    try:
        value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
    except (OSError, ValueError) as exc:
        raise ValueError(f'{path}: cannot read valid JSON') from None
    if not isinstance(value, dict):
        raise ValueError(f'{path}: expected a JSON object')
    return value


def merge_json(template, current, field='$'):
    if current is None:
        return None
    if isinstance(template, dict) != isinstance(current, dict):
        raise ValueError(f'{field}: object/scalar type conflict')
    if isinstance(template, dict):
        result = copy.deepcopy(current)
        for key, default in template.items():
            result[key] = (merge_json(default, current[key], f'{field}.{key}')
                           if key in current else copy.deepcopy(default))
        return result
    if template is not None:
        numeric = type(template) in (int, float) and type(current) in (int, float)
        if not numeric and type(template) is not type(current):
            raise ValueError(f'{field}: configuration type conflict')
    return copy.deepcopy(current)


def configuration_updates(root):
    # Import only on setup's YAML path. compose_project imports read_json before
    # adding source paths, and host down must work without the YAML dependency.
    from robot_control.config_paths import main_config_targets, is_main_json
    from robot_control.yaml_config import load_yaml_config, render_yaml_config, strict_equal
    root = Path(root).resolve()
    mains = main_config_targets(root)  # Reject legacy main files before preparing any changes.
    files = [*(root / 'configs').rglob('*.json*'), *(root / 'policy').rglob('*.json*')]
    business = sorted({p.with_suffix('') if p.name.endswith('.json.example') else p
                       for p in files if p.name.endswith(('.json', '.json.example'))
                       and '.runtime' not in p.parts and not is_main_json(root, p)})
    changes, documents = [], {}
    for target, template in mains + [(p, p.with_name(p.name + '.example')) for p in business]:
        if target.is_symlink() or any(p.is_symlink() for p in target.parents if p != root.parent):
            raise ValueError(f'{target}: symlink configuration is not supported')
        yaml = target.suffix in ('.yaml', '.yml')
        read = load_yaml_config if yaml else read_json
        current = read(target) if target.exists() else None
        template = template if template is not None and template.is_file() else None
        default = read(template) if template is not None else None
        value = merge_json(default, current, str(target.relative_to(root))) if current is not None and default is not None else (current if current is not None else default)
        documents[target.relative_to(root).as_posix()] = value
        if not strict_equal(current, value):
            content = (render_yaml_config(target, value, template=template) if yaml else
                       (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode())
            changes.append((target, content))
    return changes, documents


def _private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ValueError(f'{path}: symlink output is not supported')
    os.chmod(path, 0o700)


def atomic_bytes(path, content, mode=0o600):
    path = Path(path)
    if path.is_symlink():
        raise ValueError(f'{path}: symlink output is not supported')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_updates(root, changes):
    root = Path(root).resolve()
    changes = [(Path(path), content) for path, content in changes
               if not Path(path).exists() or Path(path).read_bytes() != content]
    if not changes:
        return
    for path, _ in changes:
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f'{path}: invalid configuration destination')
    existing = [(path, path.read_bytes()) for path, _ in changes if path.exists()]
    if existing:
        backup = root / '.setup/backups' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
        _private_directory(root / '.setup')
        _private_directory(root / '.setup/backups')
        _private_directory(backup)
        for path, content in existing:
            target = backup / path.relative_to(root)
            _private_directory(target.parent)
            atomic_bytes(target, content)
    for path, content in changes:
        atomic_bytes(path, content)


def parse_env(content):
    """Parse assignment data without shell execution or variable expansion."""
    result, index = {}, 0
    lines = content.splitlines(keepends=True)
    while index < len(lines):
        line = lines[index].strip()
        index += 1
        if not line or line.startswith('#'):
            continue
        match = re.match(r'(?:export\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.*)', line)
        if not match:
            raise ValueError(f'environment file: invalid assignment at line {index}')
        key, raw = match.groups()
        if raw.startswith(('"', "'")):
            quote = raw[0]
            while True:
                end = next((i for i in range(1, len(raw)) if raw[i] == quote and
                            len(raw[:i]) - len(raw[:i].rstrip('\\')) & 1 == 0), None)
                if end is not None:
                    tail = raw[end + 1:].strip()
                    if tail and not tail.startswith('#'):
                        raise ValueError(f'environment file: invalid quoted assignment for {key}')
                    value = raw[1:end]
                    if quote == '"':
                        value = re.sub(r'\\([nrt"\\])', lambda m: {'n':'\n','r':'\r','t':'\t','"':'"','\\':'\\'}[m[1]], value)
                    else:
                        value = value.replace("\\'", "'")
                    break
                if index == len(lines):
                    raise ValueError(f'environment file: unclosed quoted assignment for {key}')
                raw += '\n' + lines[index].rstrip('\r\n')
                index += 1
        else:
            value = re.split(r'\s+#', raw, maxsplit=1)[0].strip()
        result[key] = value
    return result


def merge_rc_env(example, existing, uid, gid):
    values, defaults = parse_env(existing), parse_env(example)
    additions = [f'{key}={json.dumps(value, ensure_ascii=False)}' for key, value in defaults.items()
                 if key not in values and key not in ('RC_UID', 'RC_GID')]
    for key, value in (('RC_UID', str(uid)), ('RC_GID', str(gid))):
        if values.get(key) != value:
            additions.append(f'{key}={value}')
    if not additions:
        return existing
    return existing + ('' if not existing or existing.endswith('\n') else '\n') + '\n'.join(additions) + '\n'
