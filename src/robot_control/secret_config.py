"""Copies for public output; actual configuration remains intact for workers/save."""
import ast
import copy
import json
import re


def public_value(value):
    if isinstance(value, dict):
        sensitive_patch = 'api_key' in str(value.get('path', '')).split('.')
        return {key: '***' if key == 'api_key' or (key == 'value' and sensitive_patch)
                else public_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [public_value(item) for item in value]
    if isinstance(value, str):
        return public_message(value)
    return copy.deepcopy(value)


def public_message(value):
    if not isinstance(value, str):
        return json.dumps(public_value(value), ensure_ascii=False)
    if '{' not in value and '[' not in value:
        return value
    decoder = json.JSONDecoder()
    for match in re.finditer(r'[\[{]', value):
        try:
            parsed, end = decoder.raw_decode(value[match.start():])
        except ValueError:
            fragment = _literal_prefix(value[match.start():])
            if fragment is None:
                continue
            try:
                parsed = ast.literal_eval(fragment)
                end = len(fragment)
            except (ValueError, SyntaxError, RecursionError):
                continue
        clean = public_value(parsed)
        if clean != parsed:
            return value[:match.start()] + json.dumps(clean, ensure_ascii=False) + public_message(value[match.start() + end:])
    # Also hide a recognizable key in malformed JSON without echoing its value.
    return re.sub(r'''((?:"api_key"|'api_key')\s*:\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^,}\]\n]*)''',
                  r'\1"***"', value)


def _literal_prefix(text):
    """Find one balanced repr without executing it or rescanning every suffix."""
    stack, quote, escaped = [], None, False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == quote:
                quote = None
        elif char in ('"', "'"):
            quote = char
        elif char in '[{(':
            stack.append(char)
        elif char in ']})':
            if not stack or stack.pop() != {']':'[', '}':'{', ')':'('}[char]:
                return None
            if not stack:
                return text[:index + 1]
    return None


def _contains_key(value):
    if isinstance(value, dict):
        return 'api_key' in value or any(_contains_key(item) for item in value.values())
    return isinstance(value, list) and any(_contains_key(item) for item in value)


def reject_secret_edit(line):
    if not isinstance(line, str):
        return
    parts = line.strip().split(maxsplit=2)
    if len(parts) >= 2 and parts[:2] == ['mcp', 'command']:
        nested = parts[2] if len(parts) == 3 else ''
        if nested.lstrip().startswith('{'):
            try:
                nested = json.loads(nested).get('line', '')
            except (ValueError, AttributeError):
                # Malformed wrappers must not echo a recognizable secret edit.
                if re.search(r'api(?:_|\\u005[fF])key', nested):
                    raise ValueError('密钥只能在配置文件中修改') from None
                return
        return reject_secret_edit(nested)
    if not parts or parts[0] != 'set' or len(parts) < 2:
        return
    if 'api_key' in parts[1].split('.'):
        raise ValueError('密钥只能在配置文件中修改')
    if len(parts) == 3:
        try:
            value = json.loads(parts[2])
        except ValueError:
            if re.search(r'api(?:_|\\u005[fF])key', parts[2]):
                raise ValueError('密钥只能在配置文件中修改') from None
            return
        if _contains_key(value):
            raise ValueError('密钥只能在配置文件中修改')
