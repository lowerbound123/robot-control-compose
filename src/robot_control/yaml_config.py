"""YAML main configuration I/O. Imports remain stdlib-only until YAML is used."""
from __future__ import annotations

import copy
import io
import math
import os
from pathlib import Path
import tempfile


class ConfigFormatError(ValueError):
    pass


def _error(path, category, mark=None):
    position = f':{mark.line + 1}:{mark.column + 1}' if mark is not None else ''
    return ConfigFormatError(f'{path}{position}: {category}')


def _yaml():
    from ruamel.yaml import YAML
    value = YAML(typ='rt', pure=True)
    value.preserve_quotes = True
    value.allow_duplicate_keys = False
    value.allow_unicode = True
    return value


def strict_equal(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(strict_equal(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(strict_equal(a, b) for a, b in zip(left, right))
    return left == right


def _plain(value):
    from ruamel.yaml.scalarbool import ScalarBoolean
    from ruamel.yaml.comments import TaggedScalar
    if isinstance(value, TaggedScalar):
        # Explicit standard !!str tags can produce a TaggedScalar in roundtrip mode.
        if str(value.tag) == 'tag:yaml.org,2002:str':
            return str(value.value)
        raise ValueError('unsupported scalar')
    if value is None:
        return None
    if isinstance(value, (bool, ScalarBoolean)):
        return bool(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float) and math.isfinite(value):
        return float(value)
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    raise ValueError('non-JSON value')


def _document(text, path):
    from ruamel.yaml.error import YAMLError
    from ruamel.yaml.nodes import MappingNode, SequenceNode
    from ruamel.yaml.tokens import DirectiveToken, AnchorToken
    allowed = {'tag:yaml.org,2002:' + kind for kind in ('map', 'seq', 'str', 'null', 'bool', 'int', 'float')}
    try:
        anchors = set()
        for token in _yaml().scan(text):
            if isinstance(token, DirectiveToken) and token.name == 'YAML' and token.value != (1, 2):
                raise _error(path, 'only YAML 1.2 is supported', token.start_mark)
            if isinstance(token, AnchorToken):
                if token.value in anchors:
                    raise _error(path, 'duplicate anchor', token.start_mark)
                anchors.add(token.value)
        node = _yaml().compose(text)
        if not isinstance(node, MappingNode):
            raise _error(path, 'expected a mapping', getattr(node, 'start_mark', None))
        # Inspect the node graph before construction: roundtrip construction can
        # replace recursive aliases with None, which must not hide invalid input.
        active = set()
        def visit(current):
            if id(current) in active:
                raise _error(path, 'cyclic alias is not supported', current.start_mark)
            if current.tag not in allowed:
                raise _error(path, 'unsupported YAML tag or merge key', current.start_mark)
            active.add(id(current))
            try:
                if isinstance(current, MappingNode):
                    for key, item in current.value:
                        if key.tag != 'tag:yaml.org,2002:str':
                            raise _error(path, 'mapping keys must be strings', key.start_mark)
                        visit(key)
                        visit(item)
                elif isinstance(current, SequenceNode):
                    for item in current.value:
                        visit(item)
            finally:
                active.remove(id(current))
        visit(node)
        document = _yaml().load(text)
        return document, _plain(document)
    except ConfigFormatError:
        raise
    except YAMLError as exc:
        raise _error(path, type(exc).__name__, getattr(exc, 'problem_mark', None)) from None
    except (ValueError, TypeError, OverflowError, RecursionError):
        raise _error(path, 'expected finite JSON-compatible values') from None


def _read(path):
    path = Path(path)
    if path.is_symlink():
        raise _error(path, 'symlink configuration is not supported')
    try:
        return path.read_text(encoding='utf-8')
    except (OSError, UnicodeError):
        raise _error(path, 'cannot read configuration') from None


def load_yaml_config(path):
    return _document(_read(path), path)[1]


def _detach_anchor(value):
    if hasattr(value, 'yaml_set_anchor'):
        value.yaml_set_anchor(None)
    return value


def _reconcile(old, value, template=None):
    from ruamel.yaml.comments import CommentedMap, CommentedSeq
    if old is not None and strict_equal(_plain(old), value):
        return old
    if isinstance(value, dict):
        existing = old if isinstance(old, dict) else {}
        result = _detach_anchor(copy.deepcopy(old)) if isinstance(old, CommentedMap) else CommentedMap()
        for key in list(result):
            if key not in value:
                del result[key]
                result.ca.items.pop(key, None)
        for key, item in value.items():
            example = template.get(key) if isinstance(template, dict) else None
            result[key] = _reconcile(existing.get(key), item, example)
            if key not in existing and hasattr(template, 'ca') and key in template.ca.items:
                result.ca.items[key] = copy.deepcopy(template.ca.items[key])
        return result
    if isinstance(value, list):
        existing = old if isinstance(old, list) else []
        result = _detach_anchor(copy.deepcopy(old)) if isinstance(old, CommentedSeq) else CommentedSeq()
        # Assign comments by stable occurrence, including duplicate chain members.
        unused = list(range(len(existing)))
        items, comments = [], {}
        for index, item in enumerate(value):
            match = next((n for n in unused if strict_equal(_plain(existing[n]), item)), None)
            if match is None:
                items.append(_reconcile(None, item))
            else:
                unused.remove(match)
                items.append(existing[match])
                if hasattr(old, 'ca') and match in old.ca.items:
                    comments[index] = copy.deepcopy(old.ca.items[match])
        result[:] = items
        result.ca.items.clear()
        result.ca.items.update(comments)
        return result
    if old is None and template is not None and strict_equal(_plain(template), value):
        return _detach_anchor(copy.deepcopy(template))
    if isinstance(value, str) and isinstance(old, str):
        return _detach_anchor(type(old)(value))
    return value


def render_yaml_config(path, values, *, template=None):
    path = Path(path)
    # Values crossing this boundary must be plain, acyclic JSON-compatible data.
    import json
    try:
        plain = json.loads(json.dumps(values, allow_nan=False, ensure_ascii=False))
        if not isinstance(values, dict) or not strict_equal(plain, values):
            raise ValueError()
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise _error(path, 'expected a plain JSON-compatible mapping') from None
    original = _read(path) if path.exists() or path.is_symlink() else None
    document, current = _document(original, path) if original is not None else (None, None)
    if original is not None and strict_equal(current, values):
        return original.encode('utf-8')
    exemplar = _document(_read(template), template)[0] if template is not None else None
    if document is None and exemplar is not None:
        document = copy.deepcopy(exemplar)
    result = _reconcile(document, values, exemplar)
    stream = io.StringIO()
    _yaml().dump(result, stream)
    output = stream.getvalue()
    if not strict_equal(_document(output, path)[1], values):
        raise _error(path, 'serialized configuration differs from target')
    return output.encode('utf-8')


def save_yaml_config(path, values):
    path = Path(path)
    data = render_yaml_config(path, values)
    if path.exists() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
