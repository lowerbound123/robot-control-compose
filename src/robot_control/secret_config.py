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
    # Inspect supported envelopes before parsing/logging their content. Legacy
    # private execution lines retain the same guard during controller dispatch.
    parts = line.strip().split(maxsplit=2)
    if len(parts) >= 2 and parts[0] == 'mcp' and parts[1] in ('command', 'policy_config', 'tui_command'):
        raw = parts[2] if len(parts) == 3 else ''
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            if re.search(r'api(?:_|\\u005[fF])key', raw):
                raise ValueError('密钥只能在配置文件中修改') from None
            return
        if not isinstance(data, dict):
            return
        if parts[1] == 'command':
            return reject_secret_edit(data.get('line', ''))
        if parts[1] == 'tui_command':
            batch = data.get('text', '')
            if isinstance(batch, str):
                for command in batch.splitlines():
                    reject_secret_edit(command)
            return
        if data.get('action') == 'set':
            return _reject_secret_value(data.get('key', ''), data.get('value_text', ''))
        return
    tokens = line.strip().split(maxsplit=5)
    if len(tokens) >= 4 and tokens[0] == 'policy' and tokens[2:4] == ['config', 'set']:
        return _reject_secret_value(tokens[4] if len(tokens) > 4 else '', tokens[5] if len(tokens) > 5 else '')
    tokens = line.strip().split(maxsplit=4)
    if tokens[:3] == ['config', 'rc', 'set']:
        return _reject_secret_value(tokens[3] if len(tokens) > 3 else '', tokens[4] if len(tokens) > 4 else '')
    tokens = line.strip().split(maxsplit=3)
    if tokens[:2] == ['global', 'set']:
        return _reject_secret_value(tokens[2] if len(tokens) > 2 else '', tokens[3] if len(tokens) > 3 else '')
    if parts and parts[0] == 'set' and len(parts) >= 2:
        return _reject_secret_value(parts[1], parts[2] if len(parts) > 2 else '')


def _reject_secret_value(key, text):
    if isinstance(key, str) and 'api_key' in key.split('.'):
        raise ValueError('密钥只能在配置文件中修改')
    if not isinstance(text, str):
        return
    try:
        value = json.loads(text)
    except ValueError:
        if re.search(r'api(?:_|\\u005[fF])key', text):
            raise ValueError('密钥只能在配置文件中修改') from None
        return
    if _contains_key(value):
        raise ValueError('密钥只能在配置文件中修改')
